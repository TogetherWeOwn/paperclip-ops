"""Immutable offline acceptance contract for db_backup_stall.sh.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_backup_observation_contract

The additive JSON seam below is deliberately small: schemaVersion (positive
integer), codes (stable reason IDs), next_action (nonempty guidance), signals
keyed by suppression/orphan/freshness, and producers keyed by in_server/host
with a selected boolean. Each signal has state measured/unknown/not_evaluated
and its own codes. Codes tested here are directory_unmeasured, stat_failed,
mtime_invalid, mtime_future, and producer_excluded. Extra fields/codes are OK;
legacy fields, prose verdicts, thresholds and exit meanings remain intact.
These IDs describe observations, not authorization, remediation execution,
clock-skew grace, monitor rearming, or a deployment claim.

Safety: sanitized environment; closed command PATH; fixed clock; real fixture
files and real entrypoint (read-only inherited fd, also usable after dropping
root). Linux Landlock refuses filesystem writes, except the harness audit file and /dev/null;
seccomp refuses sockets. Scan failure is a kernel READ_DIR denial, not a mocked
verdict or chmod under root. Stat failures are injected only at the stat seam.
Unsupported containment fails closed before the entrypoint runs. No credentials,
production paths, health requests, board writes, or implementation imports.
"""

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
TOOL = HERE / "db_backup_stall.sh"
NOW = 1_800_000_000
FRESH = "paperclip-20270115-080000.sql.gz"
HOST = "paperclip-20270115-080000-hostcron.sql.gz"
ORPHAN = "paperclip-20270115-060000.sql"
HOST_ORPHAN = "paperclip-20270115-060000-hostcron.sql"
COMPLETE = "[08:00:00] INFO: Automatic database backup complete: fixture.sql.gz\n"
SKIP = "[09:00:00] WARN: Skipping scheduled database backup because a previous backup is still running\n"


# These adapters contain no detector logic: date fixes the clock, stat either
# delegates to the OS or returns a specified syscall-boundary fault.
ADAPTER = r'''#!/usr/bin/python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
cfg = json.loads(pathlib.Path("adapters.json").read_text())
def audit(kind):
    with open("audit", "a") as f:
        f.write(json.dumps({"kind": kind, "command": name, "args": args}) + "\n")
if name == "date" and args == ["-u", "+%s"]:
    print(1800000000)
    sys.exit(0)
if name == "stat" and cfg.get("fault"):
    target = cfg["target"]
    if any(pathlib.Path(a).name == target for a in args):
        fault = cfg["fault"]
        is_size = "%s" in args or "--format=%s" in args
        is_mtime = "%Y" in args or "--format=%Y" in args
        if (fault == "size_failed" and is_size) or (fault == "mtime_failed" and is_mtime):
            audit("injected")
            sys.stderr.write("stat: fixture observation failed\n")
            sys.exit(1)
        if fault in ("mtime_invalid", "mtime_empty") and is_mtime:
            audit("injected")
            print("not-a-time" if fault == "mtime_invalid" else "")
            sys.exit(0)
if name not in cfg["allowed"]:
    audit("blocked")
    sys.exit(97)
os.execv(cfg["allowed"][name], [name] + args)
'''

GUARD = r'''set -T
command_not_found_handle() {
  printf '{"kind":"blocked","command":"not_found"}\n' >> audit
  return 97
}
_observation_guard() {
  case "$1" in
    /*|./*|../*|exec\ /*|command\ /*)
      printf '{"kind":"blocked","command":"absolute_command"}\n' >> audit
      exit 97 ;;
  esac
}
trap '_observation_guard "$BASH_COMMAND"' DEBUG
'''


def contain(scan_failed=False):
    """Child-only Linux containment; failure aborts exec rather than running naked."""
    # A root CI invocation must not make chmod(0) fixtures readable. The cwd and
    # entrypoint fd are already open, so no world-searchable checkout is needed.
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)
    libc = ctypes.CDLL(None, use_errno=True)

    def checked(result, operation):
        if result < 0:
            raise OSError(ctypes.get_errno(), operation)
        return result

    checked(libc.prctl(38, 1, 0, 0, 0), "no_new_privs")
    abi = checked(libc.syscall(444, 0, 0, 1), "Landlock ABI")
    # WRITE_FILE, REMOVE_DIR/FILE, MAKE_*; REFER and TRUNCATE when supported.
    writes = (1 << 1) | sum(1 << b for b in range(4, 13))
    if abi >= 2:
        writes |= 1 << 13
    if abi >= 3:
        writes |= 1 << 14
    else:
        raise RuntimeError("Landlock ABI >= 3 required to deny truncation")
    handled = writes | ((1 << 3) if scan_failed else 0)
    attr = ctypes.c_uint64(handled)
    ruleset = checked(libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0), "Landlock create")

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    grants = [("audit", 1 << 1), ("/dev/null", (1 << 1) | (1 << 14))]
    if scan_failed:
        # Python's module loader enumerates standard-library directories. Keep
        # adapters operational while denying scans of the fixture tree only.
        grants.extend((path, 1 << 3) for path in ("/usr", "/lib", "/lib64", "/bin", "/etc")
                      if os.path.isdir(path))
    for path, access in grants:
        path_fd = os.open(path, os.O_PATH)
        rule = PathRule(access, path_fd)
        checked(libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0), "Landlock output rule")
        os.close(path_fd)
    # No fixture READ_DIR grant in scan-failure mode: readdir fails even though
    # the directory is readable/searchable and ordinary stat still works.
    checked(libc.syscall(446, ruleset, 0), "Landlock restrict")
    os.close(ruleset)

    deny_sockets()


def deny_sockets():
    """Child-only socket fence, also reusable for the existing shell suite."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) < 0:
        raise OSError(ctypes.get_errno(), "no_new_privs")
    # Classic BPF seccomp: forbid socket/socketpair creation and connect. This
    # also protects against an accidentally absolute network executable.
    numbers = {"x86_64": (41, 42, 53), "aarch64": (198, 199, 203)}
    if platform.machine() not in numbers:
        raise RuntimeError("No safe socket filter for this architecture")

    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                    ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]

    class Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]

    instructions = [Filter(0x20, 0, 0, 0)]  # seccomp_data.nr
    for number in numbers[platform.machine()]:
        instructions.extend([Filter(0x15, 0, 1, number), Filter(0x06, 0, 0, 0x50000 | errno.EPERM)])
    instructions.append(Filter(0x06, 0, 0, 0x7FFF0000))
    filters = (Filter * len(instructions))(*instructions)
    program = Program(len(filters), filters)
    if libc.prctl(22, 2, ctypes.byref(program), 0, 0) < 0:
        raise OSError(ctypes.get_errno(), "seccomp socket filter")


class BackupObservationContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="backup-observation-", dir=os.environ.get("TMPDIR"))
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        for name in ("archives", "bin", "tmp", "home"):
            (self.root / name).mkdir()
            (self.root / name).chmod(0o755)
        self.write("server.log", COMPLETE)
        self.write("audit", "")
        (self.root / "audit").chmod(0o666)
        self.write("guard.sh", GUARD)
        self.write("adapter.py", ADAPTER)
        (self.root / "adapter.py").chmod(0o755)
        allowed = {}
        for name in ("tail", "grep", "stat", "date", "cut", "basename", "cat", "find", "ls", "sort", "head", "wc", "sed", "awk", "tr", "readlink", "dirname", "python3"):
            path = shutil.which(name, path="/usr/bin:/bin")
            self.assertIsNotNone(path, f"Required local fixture tool: {name}")
            allowed[name] = path
        self.config = {"allowed": allowed}
        denied = ("curl", "wget", "ssh", "scp", "sftp", "docker", "podman", "systemctl", "journalctl",
                  "psql", "sqlite3", "gh", "git", "node", "npm", "npx", "nc", "socat", "rm", "mkdir",
                  "mv", "cp", "touch", "chmod", "chown", "tee", "mktemp", "env")
        for name in (*allowed, *denied):
            (self.root / "bin" / name).symlink_to("../adapter.py")
        self.addCleanup(self.cleanup)

    def cleanup(self):
        (self.root / "archives").chmod(0o755)
        self.temp.cleanup()

    def write(self, name, content):
        path = self.root / name
        path.write_text(content)
        path.chmod(0o644)
        os.utime(path, (NOW, NOW))
        return path

    def archive(self, name=FRESH, age=30, size=4096):
        path = self.root / "archives" / name
        path.write_bytes(b"x" * size)  # Detector's contract is size/mtime, not gzip validation.
        path.chmod(0o644)
        os.utime(path, (NOW - age * 60, NOW - age * 60))
        return path

    def snapshot(self):
        result = {}
        for path in self.root.rglob("*"):
            name = str(path.relative_to(self.root))
            if name == "audit":
                continue
            st = path.lstat()
            value = os.readlink(path) if path.is_symlink() else None
            if path.is_file() and not path.is_symlink():
                value = hashlib.sha256(path.read_bytes()).hexdigest()
            result[name] = (st.st_mode, st.st_size, st.st_mtime_ns, value)
        return result

    def invoke(self, *args, fault=None, target=FRESH, scan_failed=False, unreadable=False,
               json_output=True, no_health=True):
        self.config.update(fault=fault, target=target)
        self.write("adapters.json", json.dumps(self.config))
        self.write("audit", "").chmod(0o666)
        before = self.snapshot()
        if unreadable:
            (self.root / "archives").chmod(0)
        env = {"PATH": "./bin", "HOME": "./home",
               "TMPDIR": "./tmp", "TZ": "UTC", "LC_ALL": "C",
               "BASH_ENV": "./guard.sh", "PYTHONDONTWRITEBYTECODE": "1",
               "PAPERCLIP_API_URL": "https://health-stub.invalid"}
        # No API key (including inherited harness keys), real HOME or config.
        # Relative arguments keep unreadable/root fixtures independent of the
        # permissions of the run-owned scratch directory's ancestors.
        with TOOL.open("rb") as entrypoint:
            command = ["/bin/bash", f"/dev/fd/{entrypoint.fileno()}", "--log", "server.log", "--backup-dir", "archives"]
            if no_health:
                command.append("--no-health")
            if json_output:
                command.append("--json")
            command.extend(args)
            process = subprocess.Popen(command, cwd=self.root, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, pass_fds=(entrypoint.fileno(),),
                                       start_new_session=True, preexec_fn=lambda: contain(scan_failed))
            try:
                stdout, stderr = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                self.fail("Entrypoint exceeded the 8s offline fixture bound")
            finally:
                (self.root / "archives").chmod(0o755)
        self.assertEqual(self.snapshot(), before, "Observer wrote fixture/state/config files")
        events = [json.loads(line) for line in (self.root / "audit").read_text().splitlines()]
        self.assertFalse([e for e in events if e["kind"] == "blocked"], f"Unexpected host/network/write command: {events}")
        if fault:
            self.assertTrue([e for e in events if e["kind"] == "injected"], f"Fault seam was not exercised: {fault}")
        details = f"exit={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
        if not json_output:
            return process.returncode, stdout + stderr
        try:
            data = json.loads(stdout)
        except ValueError:
            self.fail(f"Expected one JSON object, not a shell arithmetic/tool crash: {details}")
        self.assertIsInstance(data, dict, details)
        self.assertEqual(data.get("exitCode"), process.returncode, details)
        self.assertEqual(data.get("healthStatus"), "not_checked", details)
        return process.returncode, data

    def verdict(self, result, code, verdict):
        rc, data = result
        self.assertEqual(rc, code, data)
        self.assertEqual(data.get("verdict"), verdict, data)
        return data

    def machine(self, data, state="measured", code=None, signal_id="freshness"):
        self.assertIs(type(data.get("schemaVersion")), int, "Add an integer schemaVersion without removing legacy JSON")
        self.assertGreaterEqual(data["schemaVersion"], 1)
        self.assertIsInstance(data.get("next_action"), str)
        self.assertTrue(data["next_action"].strip())
        self.assertIsInstance(data.get("codes"), list)
        signals = data.get("signals")
        self.assertIsInstance(signals, dict, "Expose independently measured/unknown signals")
        for name in ("suppression", "orphan", "freshness"):
            self.assertIn(name, signals)
            self.assertIn(signals[name].get("state"), ("measured", "unknown", "not_evaluated"))
            self.assertIsInstance(signals[name].get("codes"), list)
            for reason in signals[name]["codes"]:
                self.assertRegex(reason, r"^[a-z][a-z0-9_]*$")
        self.assertEqual(signals[signal_id]["state"], state, data)
        producers = data.get("producers")
        self.assertIsInstance(producers, dict)
        for name in ("in_server", "host"):
            self.assertIn(name, producers)
            self.assertIs(type(producers[name].get("selected")), bool)
        if code:
            self.assertIn(code, data["codes"], data)
            self.assertIn(code, signals[signal_id]["codes"], data)
        return signals

    def test_safety_fences_deny_real_writes_sockets_and_directory_scan(self):
        import sys
        probe = r'''
import errno, os, socket
assert os.access("archives", os.R_OK | os.X_OK)
for operation in (lambda: open("unexpected-state", "w"),
                  lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                  lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM),
                  lambda: os.listdir("archives")):
    try:
        operation()
    except OSError as error:
        assert error.errno in (errno.EACCES, errno.EPERM), error
    else:
        raise AssertionError("Containment did not refuse an unsafe operation")
'''
        process = subprocess.run([sys.executable, "-c", probe], cwd=self.root,
                                 env={"PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C"},
                                 capture_output=True, text=True, timeout=5,
                                 preexec_fn=lambda: contain(scan_failed=True))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertFalse((self.root / "unexpected-state").exists())

    def test_current_anchor_cannot_clear_absent_directory(self):
        data = self.verdict(self.invoke("--backup-dir", "absent"), 2, "inconclusive")
        self.machine(data, "unknown", "directory_unmeasured")

    def test_staleness_only_absent_directory_is_unmeasured(self):
        data = self.verdict(self.invoke("--staleness-only", "--backup-dir", "absent"), 2, "inconclusive")
        self.machine(data, "unknown", "directory_unmeasured")

    def test_unreadable_directory_is_not_a_measured_empty_directory(self):
        for args in ((), ("--staleness-only",)):
            with self.subTest(args=args):
                data = self.verdict(self.invoke(*args, unreadable=True), 2, "inconclusive")
                self.machine(data, "unknown", "directory_unmeasured")

    def test_failed_readdir_is_not_a_measured_empty_directory(self):
        self.archive()
        for args in ((), ("--staleness-only",)):
            with self.subTest(args=args):
                data = self.verdict(self.invoke(*args, scan_failed=True), 2, "inconclusive")
                self.machine(data, "unknown", "directory_unmeasured")

    def test_measured_empty_directory_is_a_positive_stall_control(self):
        for args in ((), ("--staleness-only",)):
            with self.subTest(args=args):
                data = self.verdict(self.invoke(*args), 1, "stall")
                self.assertTrue(any("NO USABLE BACKUP" in r for r in data["reasons"]), data)

    def test_fresh_archive_is_a_positive_health_control(self):
        self.archive()
        for args in ((), ("--staleness-only",)):
            with self.subTest(args=args):
                data = self.verdict(self.invoke(*args), 0, "ok")
                self.assertEqual(data["newestUsableBackup"], FRESH)
                self.assertEqual(data["newestUsableBackupAgeMinutes"], 30)
                self.assertEqual(data["minBackupBytes"], 1024)
                self.assertEqual(data["maxBackupAgeHours"], 2)
                self.assertEqual(data["reasons"], [])

    def test_machine_schema_is_additive_on_a_healthy_result(self):
        self.archive()
        data = self.verdict(self.invoke(), 0, "ok")
        self.machine(data)
        self.assertTrue(data["windowAnchored"])
        self.assertTrue(data["producers"]["in_server"]["selected"])
        self.assertFalse(data["producers"]["host"]["selected"])

    def test_stale_archive_is_a_positive_stall_control(self):
        self.archive(age=180)
        data = self.verdict(self.invoke(), 1, "stall")
        self.assertEqual(data["newestUsableBackupAgeMinutes"], 180)
        self.assertTrue(any("STALE" in r for r in data["reasons"]))

    def test_age_and_size_thresholds_retain_their_meanings(self):
        self.archive(age=121, size=1024)
        self.verdict(self.invoke(), 1, "stall")
        self.verdict(self.invoke("--max-backup-age-hours", "3"), 0, "ok")
        self.archive(age=120, size=1024)
        self.verdict(self.invoke(), 0, "ok")  # Existing threshold is strictly over two hours.
        self.archive(size=1023)
        self.verdict(self.invoke(), 1, "stall")
        self.verdict(self.invoke("--min-backup-bytes", "1000"), 0, "ok")

    def test_future_only_archive_never_counts_as_fresh(self):
        for age in (-1440, -1, -1 / 60):  # Even one future second is not clock-skew grace.
            self.archive(age=age)
            for args in ((), ("--staleness-only",)):
                with self.subTest(age=age, args=args):
                    data = self.verdict(self.invoke(*args), 2, "inconclusive")
                    self.assertEqual(data["newestUsableBackup"], "none")
                    self.machine(data, "unknown", "mtime_future")

    def test_future_candidate_does_not_discard_valid_independent_fresh_archive(self):
        self.archive()
        self.archive("paperclip-20270115-090000.sql.gz", age=-1440)
        data = self.verdict(self.invoke(), 0, "ok")
        self.assertEqual(data["newestUsableBackup"], FRESH, data)
        self.assertEqual(data["newestUsableBackupAgeMinutes"], 30)
        self.machine(data, "measured", "mtime_future")

    def test_failed_archive_stat_is_unknown_not_absent_or_stale(self):
        self.archive()
        for fault in ("size_failed", "mtime_failed"):
            for args in ((), ("--staleness-only",)):
                with self.subTest(fault=fault, args=args):
                    data = self.verdict(self.invoke(*args, fault=fault), 2, "inconclusive")
                    self.machine(data, "unknown", "stat_failed")

    def test_invalid_or_empty_mtime_is_unknown_not_a_shell_crash(self):
        self.archive()
        for fault in ("mtime_invalid", "mtime_empty"):
            with self.subTest(fault=fault):
                data = self.verdict(self.invoke(fault=fault), 2, "inconclusive")
                self.machine(data, "unknown", "mtime_invalid")

    def test_suppression_survives_unknown_directory_or_archive(self):
        self.write("server.log", COMPLETE + SKIP)
        self.archive()
        for kwargs, args in (({}, ("--backup-dir", "absent")), ({"scan_failed": True}, ()),
                             ({"fault": "mtime_failed"}, ())):
            with self.subTest(kwargs=kwargs, args=args):
                data = self.verdict(self.invoke(*args, **kwargs), 1, "stall")
                self.assertTrue(any("SUPPRESSION" in r for r in data["reasons"]), data)
                signals = self.machine(data, "unknown")
                self.assertEqual(signals["suppression"]["state"], "measured")

    def test_positively_observed_orphan_survives_unknown_archive_freshness(self):
        self.archive(age=-1440)
        self.archive(ORPHAN, age=180)
        data = self.verdict(self.invoke(), 1, "stall")
        self.assertEqual(data["orphanCount"], 1)
        self.assertTrue(any("ORPHAN" in r for r in data["reasons"]), data)
        signals = self.machine(data, "unknown", "mtime_future")
        self.assertEqual(signals["orphan"]["state"], "measured")

    def test_failed_orphan_stat_cannot_be_fabricated_as_an_ancient_orphan(self):
        self.archive()
        self.archive(ORPHAN, age=180)
        data = self.verdict(self.invoke(fault="mtime_failed", target=ORPHAN), 2, "inconclusive")
        self.assertEqual(data["orphanCount"], 0)
        self.machine(data, "unknown", "stat_failed", signal_id="orphan")

    def test_failed_sibling_stat_cannot_be_fabricated_as_truncation(self):
        self.archive()
        self.archive(ORPHAN, age=180)
        sibling = ORPHAN + ".gz"
        self.archive(sibling, age=180)
        data = self.verdict(self.invoke(fault="size_failed", target=sibling), 2, "inconclusive")
        self.assertEqual(data["orphanCount"], 0)
        self.machine(data, "unknown", "stat_failed", signal_id="orphan")

    def test_host_orphan_is_excluded_in_default_mode(self):
        self.archive()
        self.archive(HOST_ORPHAN, age=180)
        data = self.verdict(self.invoke(), 0, "ok")
        self.assertEqual(data["orphanCount"], 0)
        self.machine(data, "measured", "producer_excluded", signal_id="orphan")
        self.assertFalse(data["producers"]["host"]["selected"])

    def test_include_all_restores_host_orphan_detection(self):
        self.archive()
        self.archive(HOST_ORPHAN, age=180)
        data = self.verdict(self.invoke("--include-all-producers"), 1, "stall")
        self.assertEqual(data["orphanCount"], 1)
        self.assertTrue(any("ORPHAN" in r for r in data["reasons"]))

    def test_custom_producer_marker_applies_to_both_scans(self):
        self.archive()
        self.archive("paperclip-20270115-060000-nightly.sql", age=180)
        self.archive("paperclip-20270115-080000-nightly.sql.gz")
        data = self.verdict(self.invoke("--exclude-suffix", "nightly"), 0, "ok")
        self.assertEqual(data["orphanCount"], 0)
        self.assertEqual(data["newestUsableBackup"], FRESH)
        self.assertEqual(data["excludeSuffix"], "nightly")
        self.machine(data, "measured", "producer_excluded", signal_id="orphan")

    def test_in_server_orphan_still_fires_and_age_threshold_is_consulted(self):
        self.archive()
        self.archive(ORPHAN, age=91)
        self.verdict(self.invoke(), 1, "stall")
        data = self.verdict(self.invoke("--orphan-min-age-minutes", "92"), 0, "ok")
        self.assertEqual(data["orphanCount"], 0)

    def test_delivered_archive_host_filter_is_not_undone(self):
        self.archive(HOST)
        data = self.verdict(self.invoke(), 1, "stall")
        self.assertEqual(data["newestUsableBackup"], "none")
        self.assertEqual(data["newestHostArchive"], HOST)
        self.assertEqual(data["newestHostArchiveAgeMinutes"], 30)
        self.verdict(self.invoke("--include-all-producers"), 0, "ok")

    def test_staleness_only_keeps_all_producers_and_skips_log_and_orphans(self):
        self.archive(HOST)
        self.archive(HOST_ORPHAN, age=180)
        self.write("server.log", COMPLETE + SKIP)
        data = self.verdict(self.invoke("--staleness-only", "--log", "missing.log"), 0, "ok")
        self.assertTrue(data["stalenessOnly"])
        self.assertFalse(data["excludeEnabled"])
        self.assertEqual(data["orphanCount"], 0)
        self.assertEqual(data["newestUsableBackup"], HOST)
        self.verdict(self.invoke("--staleness-only", "--exclude-suffix", "hostcron"), 1, "stall")

    def test_staleness_only_machine_state_discloses_unevaluated_signals(self):
        self.archive(HOST)
        data = self.verdict(self.invoke("--staleness-only"), 0, "ok")
        signals = self.machine(data)
        self.assertEqual(signals["suppression"]["state"], "not_evaluated")
        self.assertEqual(signals["orphan"]["state"], "not_evaluated")
        self.assertTrue(data["producers"]["host"]["selected"])

    def test_future_excluded_host_archive_cannot_report_negative_freshness(self):
        self.archive()
        self.archive(HOST, age=-1440)
        data = self.verdict(self.invoke(), 0, "ok")
        self.assertIn(data["newestHostArchiveAgeMinutes"], (None, -1), data)
        self.machine(data, "measured", "mtime_future")

    def test_dry_run_aliases_explicit_local_only_without_health_or_writes(self):
        self.archive()
        # No --no-health here: --dry-run itself must enforce local-only.
        dry = self.verdict(self.invoke("--dry-run", no_health=False), 0, "ok")
        local = self.verdict(self.invoke("--local-only", no_health=False), 0, "ok")
        offline = self.verdict(self.invoke(), 0, "ok")
        for key in ("verdict", "exitCode", "newestUsableBackup", "newestUsableBackupAgeMinutes", "healthStatus"):
            self.assertEqual(dry[key], local[key], key)
            self.assertEqual(local[key], offline[key], key)

    def test_human_output_retains_inconclusive_meaning_for_unmeasured_freshness(self):
        rc, text = self.invoke("--backup-dir", "absent", json_output=False)
        self.assertEqual(rc, 2, text)
        self.assertIn("INCONCLUSIVE", text)
        self.assertNotIn("No backup stall detected", text)


if __name__ == "__main__":
    unittest.main()
