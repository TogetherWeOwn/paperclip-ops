"""Immutable offline acceptance contract for coolify_capacity_headroom.sh.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_capacity_evidence_contract
CLI examples (synthetic fixtures only):
  ./coolify_capacity_headroom.sh --jobs sample.json
  ./coolify_capacity_headroom.sh --jobs sample.json --json --dry-run
  ./coolify_capacity_headroom.sh --jobs sample.json --min-attempts 7 --min-hours 24

This file belongs to the Test Author; implementation changes must not edit it.
It specifies reporting, NOT permission to change pools, capture live jobs, or
remove hosts. Defaults remain 24 hours / 10 attempts per compared arm and the
existing 2 + 4 nominal slots. Shared slots are logical slots, not CPU cores.

Version 1 wire contract (additional fields are allowed):
* --json emits one object: schema_version=1, verdict (GO/NO-GO/INCONCLUSIVE
  or ERROR), code, retryable (bool), next_action (nonempty string).
* Successful reports include counts (input_rows, accepted_rows,
  excluded_no_runner, excluded_clock, malformed_rows, duplicate_rows,
  unknown_rows), arms.coolify / arms.garm (matched_attempts, observed_hours,
  adequate, complete), window.start/end, evidence_complete, job_ids,
  cohort_id (null if not supplied), assumptions, and capacity
  (nominal_slots, candidate_peak, incumbent_peak, combined_peak,
  residual_headroom_known). Unknown combined_peak is null, never zero-filled.
* Codes: MEASURED_FIT, CAPACITY_EXCEEDED, INSUFFICIENT_EVIDENCE;
  MISSING_OPTION_VALUE, INVALID_OPTION_VALUE, UNSUPPORTED_OPTION,
  INVALID_JOBS, UNREADABLE_INPUT, NO_COMPUTE. Input errors exit 2; measured
  NO-GO and thin-but-readable INCONCLUSIVE remain reports with exit 0.
* The existing bare array / {jobs: [...]} forms remain accepted. Neither
  asserts completeness. For a measured residual verdict the fixture supplies
  {jobs, evidence: {cohort_id, complete, window: {start, end},
  arms: ["coolify", "garm"]}}. Each comparable job carries that cohort_id.
  This is an explicit caller assertion, NOT proof of live inventory. Omitted,
  false, contradictory, or mismatched provenance cannot authorize a GO.
* Valid runner intervals use [start, end): an end/start tie is not overlap.
  No-runner queue/cancel spans are excluded, not malformed compute. IDs, if
  supplied, are positive integer job IDs; duplicates cannot inflate adequacy.
  Malformed/excluded/unknown evidence must remain visible, never silently
  establish completeness. Claims about residual capacity state assumptions.

All clocks, IDs, names and evidence are synthetic. The real entrypoint runs
under a Linux Landlock read-only filesystem and a seccomp network fence, with
an empty credential environment and an executable allowlist. No fallback to
an unsandboxed subprocess is permitted. Linux ABI >=3 is required; unsupported
platforms FAIL CLOSED. Only the parent test harness writes run-owned fixtures
and captures; the report process cannot write any file or read HOME/secrets.
"""

import copy
import ctypes
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import signal
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "coolify_capacity_headroom.sh"
START = "2026-01-01T00:00:00Z"
END = "2026-01-02T02:00:00Z"
COHORT = "synthetic-capacity-trial-v1"
VALUE_FLAGS = ("--jobs", "--node7b1ff338-max", "--node575e13db-max",
               "--min-hours", "--min-attempts")


def job(job_id, runner, start, end, **extra):
    return dict(id=job_id, name=f"synthetic-{job_id}", runner_name=runner,
                started_at=start, completed_at=end, conclusion="success",
                labels=["self-hosted"], cohort_id=COHORT, **extra)


def sample(attempts=10):
    """Each arm has ten distinct attempts spanning 26h; maxima do not overlap."""
    jobs = []
    for i in range(attempts):
        day = "02" if i == attempts - 1 else "01"
        hour = 1 if i == attempts - 1 else i
        jobs.append(job(100 + i, f"coolify-vps-{i}",
                        f"2026-01-{day}T{hour:02}:00:00Z",
                        f"2026-01-{day}T{hour:02}:10:00Z"))
        jobs.append(job(200 + i, f"garm-synthetic-{i}",
                        f"2026-01-{day}T{hour:02}:20:00Z",
                        f"2026-01-{day}T{hour:02}:30:00Z"))
    # Arm-local spans exceed 24h, rather than borrowing the envelope window.
    jobs[-2]["completed_at"] = END
    jobs[-1]["completed_at"] = END
    return {"jobs": jobs, "evidence": {
        "cohort_id": COHORT, "complete": True,
        "window": {"start": START, "end": END},
        "arms": ["coolify", "garm"],
    }}


def shared_load(overlap=True, tie=False):
    data = sample()
    # Baseline last intervals overlap at peak 2. Here the shared-slot maximum
    # is 3 only when one additional candidate overlaps both incumbents.
    data["jobs"] += [
        job(301, "garm-synthetic-a", "2026-01-01T12:00:00Z", "2026-01-01T12:10:00Z"),
        job(302, "garm-synthetic-b", "2026-01-01T12:00:00Z", "2026-01-01T12:10:00Z"),
    ]
    start, end = ("12:00", "12:10") if overlap else ("13:00", "13:10")
    if tie:
        start, end = "11:50", "12:00"
    data["jobs"].append(job(303, "coolify-vps-extra",
                            f"2026-01-01T{start}:00Z", f"2026-01-01T{end}:00Z"))
    return data


def sandbox(readable):
    """Constrain the child before exec; no changes to the production script."""
    libc = ctypes.CDLL(None, use_errno=True)

    def checked(result):
        if result < 0:
            raise OSError(ctypes.get_errno(), "offline sandbox unavailable")
        return result

    checked(libc.prctl(38, 1, 0, 0, 0))  # PR_SET_NO_NEW_PRIVS
    abi = checked(libc.syscall(444, 0, 0, 1))  # landlock_create_ruleset VERSION
    if abi < 3:
        raise RuntimeError("Landlock ABI >=3 required; refusing unsafe fallback")
    handled = ctypes.c_uint64((1 << 15) - 1)  # through REFER and TRUNCATE
    ruleset = checked(libc.syscall(444, ctypes.byref(handled), 8, 0))

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]

    def allow(path, access):
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        try:
            rule = PathRule(access, fd)
            checked(libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0))
        finally:
            os.close(fd)

    # Read libraries, stdlib and fixtures, but execute only these audited
    # offline interpreters/utilities. curl/gh/ssh/docker/env are not executable.
    for path in ("/usr", "/bin", "/lib", "/lib64", str(ROOT), str(readable)):
        if Path(path).exists():
            allow(path, 4 | 8)
    for path in ("/etc/ld.so.cache", "/etc/localtime"):
        if Path(path).exists():
            allow(path, 4)
    allow("/dev/null", 4 | 2 | (1 << 14))  # discard output; no persisted writes
    for name in ("bash", "python3", "jq", "dirname", "date", "awk", "sort"):
        path = shutil.which(name, path="/usr/bin:/bin")
        if path:
            allow(str(Path(path).resolve()), 1 | 4)
    # The ELF interpreter also needs execute permission; permitting only the
    # final executable inode would prevent even the positive controls running.
    for loader in ("/lib64/ld-linux-x86-64.so.2", "/lib/ld-linux-aarch64.so.1"):
        if Path(loader).exists():
            allow(str(Path(loader).resolve()), 1 | 4)
    checked(libc.syscall(446, ruleset, 0))  # landlock_restrict_self
    os.close(ruleset)

    # Reject any socket creation/connect before it happens, even via an
    # absolute command path, a Python import or a shell /dev/tcp redirection.
    arch, calls = {
        "x86_64": (0xC000003E, (41, 42, 43, 49, 50, 53, 288)),
        "aarch64": (0xC00000B7, (198, 199, 200, 201, 202, 203, 242)),
    }[platform.machine()]

    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                    ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]

    class Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]

    instructions = [(0x20, 0, 0, 4), (0x15, 1, 0, arch),
                    (0x06, 0, 0, 0x80000000), (0x20, 0, 0, 0)]
    for call in calls:
        # EPERM also lets libc fall back from its optional local NSS socket;
        # neither that socket nor a network socket is ever created.
        instructions += [(0x15, 0, 1, call), (0x06, 0, 0, 0x00050001)]
    instructions += [(0x06, 0, 0, 0x7FFF0000)]
    filters = (Filter * len(instructions))(*(Filter(*i) for i in instructions))
    program = Program(len(filters), filters)
    checked(libc.prctl(22, 2, ctypes.byref(program), 0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024, 256 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))


class CapacityEvidenceContract(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("PAPERCLIP_SCRATCH_DIR")
        self.work = tempfile.TemporaryDirectory(prefix="capacity-contract-", dir=scratch)
        self.addCleanup(self.work.cleanup)
        self.path = Path(self.work.name)
        (self.path / "home").mkdir()
        # A private-looking synthetic file: the sandbox must refuse it, not
        # merely avoid transmitting it. Never copy any real credential here.
        self.private = self.path / "home" / "credential-canary"
        self.private.write_text("synthetic-not-a-real-credential")
        self.inputs = self.path / "inputs"
        self.inputs.mkdir()
        self.number = 0

    def invoke(self, args, command=None, deadline=2):
        self.number += 1
        out = self.path / f"stdout-{self.number}"
        err = self.path / f"stderr-{self.number}"
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.path / "home"),
               "TMPDIR": str(self.inputs), "LC_ALL": "C", "TZ": "UTC",
               "PYTHONDONTWRITEBYTECODE": "1"}
        cmd = command or [shutil.which("bash"), "--noprofile", "--norc", str(TOOL)]
        with out.open("wb") as stdout, err.open("wb") as stderr:
            process = subprocess.Popen(
                [*cmd, *args], cwd=self.inputs, env=env, stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, start_new_session=True,
                preexec_fn=lambda: sandbox(self.inputs))
            try:
                process.wait(timeout=deadline)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                self.fail(f"CLI did not terminate in {deadline}s: {args!r}; "
                          f"stderr tail={err.read_bytes()[-300:]!r}")
        return subprocess.CompletedProcess(cmd + args, process.returncode,
                                           out.read_text(), err.read_text())

    def report(self, data, *options, machine=False):
        fixture = self.inputs / f"sample-{self.number}.json"
        fixture.write_text(json.dumps(data))
        before = fixture.read_bytes()
        result = self.invoke(["--jobs", str(fixture), *options, *(["--json"] if machine else [])])
        self.assertEqual(fixture.read_bytes(), before, "report modified its evidence")
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        return result

    def wire(self, result, verdict=None, code=None, exit_code=0):
        self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
        try:
            body = json.loads(result.stdout)
        except (ValueError, TypeError):
            self.fail(f"expected one JSON report; stdout={result.stdout!r}; stderr={result.stderr!r}")
        self.assertIsInstance(body, dict)
        self.assertEqual(body.get("schema_version"), 1)
        self.assertIs(type(body.get("retryable")), bool)
        self.assertIsInstance(body.get("next_action"), str)
        self.assertTrue(body["next_action"].strip())
        self.assertIsInstance(body.get("code"), str)
        if verdict:
            self.assertEqual(body.get("verdict"), verdict, body)
        if code:
            self.assertEqual(body.get("code"), code, body)
        return body

    def text_verdict(self, result, verdict):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"verdict={verdict}", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_sandbox_blocks_filesystem_writes_secret_reads_network_and_host_exec(self):
        # Harness positive controls: ensure tests cannot touch the host even if
        # a future CLI implementation accidentally calls a live adapter.
        probes = [
            "import socket; socket.socket()",
            f"open({str(self.inputs / 'unexpected-write')!r}, 'w').write('bad')",
            f"open({str(self.private)!r}).read()",
            "import subprocess; subprocess.run(['/usr/bin/touch', 'host-command'], check=True)",
        ]
        for probe in probes:
            with self.subTest(probe=probe):
                result = self.invoke(["-I", "-B", "-c", probe], command=[shutil.which("python3", path="/usr/bin:/bin")])
                self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.inputs / "unexpected-write").exists())
        self.assertFalse((self.inputs / "host-command").exists())

    def test_complete_comparable_positive_control_text(self):
        self.text_verdict(self.report(sample()), "GO")

    def test_existing_no_runner_exclusion_and_runner_classes_text(self):
        data = [
            job(1, "coolify-vps-a", START, "2026-01-01T00:10:00Z"),
            job(2, "garm-synthetic-a", START, "2026-01-01T00:05:00Z"),
            job(3, "ci-node7b1ff338-iso-a", START, "2026-01-01T00:20:00Z"),
            job(4, "", START, "2026-01-01T00:30:00Z"),
        ]
        data[-1]["conclusion"] = "cancelled"
        result = self.report(data)
        self.text_verdict(result, "INCONCLUSIVE")
        for text in ("runner_minutes_total=35.00", "coolify=10.00", "garm=5.00",
                     "node7b1ff338=20.00", "excluded_no_runner=1"):
            self.assertIn(text, result.stdout)

    def test_missing_terminal_value_for_every_value_flag_terminates(self):
        for flag in VALUE_FLAGS:
            with self.subTest(flag=flag):
                result = self.invoke([flag])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("MISSING_OPTION_VALUE", result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_flag_shaped_value_is_not_consumed_as_an_option_value(self):
        for flag in VALUE_FLAGS:
            with self.subTest(flag=flag):
                result = self.invoke(["--json", flag, "--dry-run"])
                body = self.wire(result, "ERROR", "MISSING_OPTION_VALUE", exit_code=2)
                self.assertFalse(body["retryable"])

    def test_invalid_numeric_options_refuse_without_traceback_or_zero_fill(self):
        invalid = {
            "--node7b1ff338-max": ("-1", "1.5", "NaN", "slots", ""),
            "--node575e13db-max": ("-1", "1.5", "Infinity", "slots", ""),
            "--min-attempts": ("-1", "1.5", "NaN", "attempts", ""),
            "--min-hours": ("-1", "NaN", "Infinity", "1e999", "hours", ""),
        }
        for flag, values in invalid.items():
            for value in values:
                with self.subTest(flag=flag, value=value):
                    body = self.wire(self.report(sample(), flag, value, machine=True),
                                     "ERROR", "INVALID_OPTION_VALUE", exit_code=2)
                    self.assertFalse(body["retryable"])

    def test_unknown_flags_never_activate_live_operations(self):
        for flag in ("--capture", "--apply", "--decommission", "--dry-ru", "--unknown"):
            with self.subTest(flag=flag):
                body = self.wire(self.report(sample(), flag, machine=True),
                                 "ERROR", "UNSUPPORTED_OPTION", exit_code=2)
                self.assertFalse(body["retryable"])

    def test_invalid_json_and_top_level_shapes_have_stable_input_errors(self):
        for text in ("{", "null", "42", '"jobs"', '{"jobs":{}}', '[1,2]'):
            with self.subTest(text=text):
                fixture = self.inputs / "malformed.json"
                fixture.write_text(text)
                result = self.invoke(["--jobs", str(fixture), "--json"])
                self.wire(result, "ERROR", "INVALID_JOBS", exit_code=2)
                self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_missing_file_is_refusal_not_transport_failure(self):
        result = self.invoke(["--jobs", str(self.inputs / "absent.json"), "--json"])
        body = self.wire(result, "ERROR", "UNREADABLE_INPUT", exit_code=2)
        self.assertFalse(body["retryable"])

    def test_empty_and_no_runner_samples_are_unmeasured_not_healthy(self):
        for data in ([], {"jobs": []}, [job(1, "", START, END)]):
            with self.subTest(data=data):
                self.wire(self.report(data, machine=True), "ERROR", "NO_COMPUTE", exit_code=2)

    def test_missing_compared_arm_cannot_borrow_other_runner_attempts_text(self):
        for arm in ("coolify", "garm"):
            data = sample()
            data["jobs"] = [j for j in data["jobs"] if j["runner_name"].startswith(arm)]
            data["jobs"] += [job(500 + i, f"unrelated-{i}", START, END) for i in range(20)]
            with self.subTest(only_arm=arm):
                self.text_verdict(self.report(data), "INCONCLUSIVE")

    def test_thin_compared_arm_cannot_borrow_global_attempts_text(self):
        data = sample()
        data["jobs"] = [j for j in data["jobs"] if not j["runner_name"].startswith("garm") or j["id"] == 200]
        self.text_verdict(self.report(data), "INCONCLUSIVE")

    def test_each_arm_needs_its_own_observed_window_text(self):
        data = sample()
        for row in data["jobs"]:
            if row["runner_name"].startswith("garm"):
                row["started_at"] = "2026-01-01T00:20:00Z"
                row["completed_at"] = "2026-01-01T00:30:00Z"
        self.text_verdict(self.report(data), "INCONCLUSIVE")

    def test_bare_and_jobs_object_inputs_do_not_imply_complete_coverage_text(self):
        rows = sample()["jobs"]
        for data in (rows, {"jobs": rows}):
            with self.subTest(shape=type(data).__name__):
                self.text_verdict(self.report(data), "INCONCLUSIVE")

    def test_nominal_slots_are_not_free_slots_text(self):
        self.text_verdict(self.report(shared_load(), "--node7b1ff338-max", "1",
                                      "--node575e13db-max", "1"), "NO-GO")

    def test_json_has_version_ids_per_arm_adequacy_window_and_assumptions(self):
        data = sample()
        body = self.wire(self.report(data, machine=True), "GO", "MEASURED_FIT")
        self.assertEqual(body["cohort_id"], COHORT)
        self.assertCountEqual(body["job_ids"], [j["id"] for j in data["jobs"]])
        self.assertEqual(body["window"], {"start": START, "end": END})
        self.assertIs(body["evidence_complete"], True)
        self.assertTrue(body["assumptions"])
        self.assertIs(body["retryable"], False)
        for key, expected in {
            "input_rows": 20, "accepted_rows": 20, "excluded_no_runner": 0,
            "excluded_clock": 0, "malformed_rows": 0, "duplicate_rows": 0,
            "unknown_rows": 0,
        }.items():
            self.assertEqual(body["counts"][key], expected, key)
        for arm in ("coolify", "garm"):
            self.assertEqual(body["arms"][arm]["matched_attempts"], 10)
            self.assertGreaterEqual(body["arms"][arm]["observed_hours"], 24)
            self.assertIs(body["arms"][arm]["adequate"], True)
            self.assertIs(body["arms"][arm]["complete"], True)
        self.assertEqual(body["capacity"]["nominal_slots"], 6)
        self.assertIs(body["capacity"]["residual_headroom_known"], True)

    def test_counts_and_remediation_describe_thin_arm_not_a_global_count(self):
        data = sample()
        data["jobs"] = [j for j in data["jobs"] if not j["runner_name"].startswith("garm") or j["id"] == 200]
        body = self.wire(self.report(data, machine=True), "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE")
        self.assertEqual(body["arms"]["coolify"]["matched_attempts"], 10)
        self.assertEqual(body["arms"]["garm"]["matched_attempts"], 1)
        self.assertIs(body["arms"]["garm"]["adequate"], False)
        self.assertIs(body["retryable"], False)  # Replaying identical evidence will not fix it.
        self.assertIn("garm", body["next_action"].lower())

    def test_configured_thresholds_are_preserved_per_arm(self):
        data = sample(attempts=7)
        cases = (((), "INCONCLUSIVE"),
                 (("--min-attempts", "7"), "GO"),
                 (("--min-attempts", "7", "--min-hours", "48"), "INCONCLUSIVE"))
        for options, verdict in cases:
            with self.subTest(options=options):
                self.text_verdict(self.report(data, *options), verdict)

    def test_candidate_peak_exceedance_positive_control_text_is_exit_zero(self):
        data = shared_load()
        for index in (304, 305):
            data["jobs"].append(job(index, f"coolify-vps-{index}",
                                    "2026-01-01T12:00:00Z", "2026-01-01T12:10:00Z"))
        self.text_verdict(self.report(data, "--node7b1ff338-max", "1",
                                      "--node575e13db-max", "1"), "NO-GO")

    def test_existing_bare_and_jobs_object_forms_have_identical_text(self):
        rows = sample()["jobs"]
        bare = self.report(rows)
        wrapped = self.report({"jobs": rows})
        self.assertEqual(bare.returncode, 0, bare.stderr)
        self.assertEqual((bare.returncode, bare.stdout), (wrapped.returncode, wrapped.stdout))

    def test_aligned_saturation_is_measured_no_go_with_exit_zero(self):
        body = self.wire(self.report(shared_load(), "--node7b1ff338-max", "1",
                                     "--node575e13db-max", "1", machine=True),
                         "NO-GO", "CAPACITY_EXCEEDED")
        self.assertEqual(body["capacity"]["candidate_peak"], 1)
        self.assertEqual(body["capacity"]["incumbent_peak"], 2)
        self.assertEqual(body["capacity"]["combined_peak"], 3)
        self.assertIs(body["capacity"]["residual_headroom_known"], True)
        self.assertIs(body["retryable"], False)

    def test_non_overlapping_maxima_are_not_summed(self):
        body = self.wire(self.report(shared_load(overlap=False), "--node7b1ff338-max", "1",
                                     "--node575e13db-max", "1", machine=True), "GO", "MEASURED_FIT")
        self.assertEqual(body["capacity"]["candidate_peak"], 1)
        self.assertEqual(body["capacity"]["incumbent_peak"], 2)
        self.assertEqual(body["capacity"]["combined_peak"], 2)

    def test_end_start_ties_are_half_open_not_false_overlap(self):
        body = self.wire(self.report(shared_load(tie=True), "--node7b1ff338-max", "1",
                                     "--node575e13db-max", "1", machine=True), "GO", "MEASURED_FIT")
        self.assertEqual(body["capacity"]["combined_peak"], 2)

    def test_incomplete_or_unidentified_samples_keep_residual_unknown(self):
        variants = []
        for field, value in (("complete", False), ("cohort_id", None), ("arms", ["coolify"])):
            data = sample()
            data["evidence"][field] = value
            variants.append(data)
        data = sample()
        data.pop("evidence")
        variants.append(data)
        data = sample()
        data["jobs"][0]["cohort_id"] = "another-synthetic-trial"
        variants.append(data)
        data = sample()
        data["jobs"][0].pop("cohort_id")
        variants.append(data)
        for index, data in enumerate(variants):
            with self.subTest(variant=index):
                body = self.wire(self.report(data, machine=True), "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE")
                self.assertIs(body["capacity"]["residual_headroom_known"], False)
                self.assertIsNone(body["capacity"]["combined_peak"])
                self.assertIs(body["evidence_complete"], False)
                self.assertTrue(body["assumptions"])

    def test_disjoint_arm_windows_are_not_comparable(self):
        data = sample()
        for row in data["jobs"]:
            if row["runner_name"].startswith("garm"):
                row["started_at"] = row["started_at"].replace("2026-01-0", "2026-01-1")
                row["completed_at"] = row["completed_at"].replace("2026-01-0", "2026-01-1")
        data["evidence"]["window"]["end"] = "2026-01-12T02:00:00Z"
        body = self.wire(self.report(data, machine=True), "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE")
        self.assertIs(body["capacity"]["residual_headroom_known"], False)

    def test_malformed_rows_types_and_ids_are_counted_before_arithmetic(self):
        mutations = [
            ("runner_name", 123), ("runner_name", ["garm-synthetic"]),
            ("id", True), ("id", -7), ("id", "not-a-job-id"),
            ("started_at", 123), ("completed_at", {}),
        ]
        for field, value in mutations:
            data = sample()
            data["jobs"][0][field] = value
            with self.subTest(field=field, value=value):
                body = self.wire(self.report(data, machine=True), "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE")
                self.assertEqual(body["counts"]["malformed_rows"], 1)
                self.assertEqual(body["counts"]["accepted_rows"], 19)
                self.assertIs(body["evidence_complete"], False)
                self.assertEqual(body["arms"]["coolify"]["matched_attempts"], 9)

    def test_bad_timestamps_do_not_become_zero_duration_or_a_go(self):
        bad_clocks = [
            (None, "2026-01-01T00:10:00Z"),
            ("not-a-time", "2026-01-01T00:10:00Z"),
            ("2026-02-30T00:00:00Z", "2026-03-01T00:10:00Z"),
            ("2026-01-01T00:00:00", "2026-01-01T00:10:00Z"),
            ("2026-01-01T00:20:00Z", "2026-01-01T00:10:00Z"),
        ]
        for start, end in bad_clocks:
            data = sample()
            data["jobs"][0].update(started_at=start, completed_at=end)
            with self.subTest(start=start, end=end):
                body = self.wire(self.report(data, machine=True), "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE")
                self.assertEqual(body["counts"]["excluded_clock"], 1)
                self.assertEqual(body["counts"]["accepted_rows"], 19)
                self.assertEqual(body["arms"]["coolify"]["matched_attempts"], 9)

    def test_queued_no_runner_and_unknown_runner_have_separate_counts(self):
        data = sample()
        queued = job(901, "", START, END)
        queued["conclusion"] = "cancelled"
        data["jobs"] += [queued, job(902, "unclassified-synthetic", START, END)]
        body = self.wire(self.report(data, machine=True), "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE")
        self.assertEqual(body["counts"]["input_rows"], 22)
        self.assertEqual(body["counts"]["excluded_no_runner"], 1)
        self.assertEqual(body["counts"]["unknown_rows"], 1)
        for arm in ("coolify", "garm"):
            self.assertEqual(body["arms"][arm]["matched_attempts"], 10)

    def test_queue_artifacts_alone_do_not_invalidate_complete_execution_coverage(self):
        data = sample()
        queued = job(901, "", START, END)
        queued["conclusion"] = "cancelled"
        data["jobs"].append(queued)
        body = self.wire(self.report(data, machine=True), "GO", "MEASURED_FIT")
        self.assertEqual(body["counts"]["accepted_rows"], 20)
        self.assertEqual(body["counts"]["excluded_no_runner"], 1)
        self.assertIs(body["evidence_complete"], True)

    def test_missing_null_and_empty_runner_spans_are_never_compute(self):
        data = sample()
        for index, runner in enumerate((None, "", "missing")):
            row = job(910 + index, runner, START, END)
            row["conclusion"] = "cancelled"
            if runner == "missing":
                row.pop("runner_name")
            data["jobs"].append(row)
        body = self.wire(self.report(data, machine=True), "GO", "MEASURED_FIT")
        self.assertEqual(body["counts"]["accepted_rows"], 20)
        self.assertEqual(body["counts"]["excluded_no_runner"], 3)
        self.assertEqual(body["capacity"]["combined_peak"], 2)

    def test_mixed_non_object_rows_are_counted_and_break_completeness(self):
        data = sample()
        data["jobs"] += [None, [], "not-a-job"]
        body = self.wire(self.report(data, machine=True), "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE")
        self.assertEqual(body["counts"]["input_rows"], 23)
        self.assertEqual(body["counts"]["accepted_rows"], 20)
        self.assertEqual(body["counts"]["malformed_rows"], 3)
        self.assertIs(body["evidence_complete"], False)

    def test_repeated_job_ids_cannot_manufacture_sample_adequacy(self):
        data = sample()
        data["jobs"] = [j for j in data["jobs"] if not j["runner_name"].startswith("garm") or j["id"] == 200]
        data["jobs"] += [copy.deepcopy(data["jobs"][1]) for _ in range(9)]
        body = self.wire(self.report(data, machine=True), "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE")
        self.assertEqual(body["arms"]["garm"]["matched_attempts"], 1)
        self.assertEqual(body["counts"]["duplicate_rows"], 9)
        self.assertEqual(body["job_ids"].count(200), 1)

    def test_unfinished_attempts_do_not_supply_completed_arm_adequacy(self):
        data = sample()
        data["jobs"][0]["conclusion"] = None
        data["jobs"][0]["completed_at"] = None
        body = self.wire(self.report(data, machine=True), "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE")
        self.assertEqual(body["arms"]["coolify"]["matched_attempts"], 9)
        self.assertIs(body["evidence_complete"], False)

    def test_claimed_window_must_contain_observed_intervals(self):
        data = sample()
        data["evidence"]["window"]["end"] = "2026-01-01T12:00:00Z"
        body = self.wire(self.report(data, machine=True), "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE")
        self.assertIs(body["capacity"]["residual_headroom_known"], False)

    def test_replay_is_deterministic_and_dry_run_is_the_same_read_only_report(self):
        fixture = self.inputs / "replay.json"
        fixture.write_text(json.dumps(shared_load()))
        before = {p.name: p.read_bytes() for p in self.inputs.iterdir()}
        args = ["--jobs", str(fixture), "--json"]
        first = self.invoke(args)
        second = self.invoke(args)
        dry = self.invoke(args + ["--dry-run"])
        self.wire(first, "GO", "MEASURED_FIT")
        self.assertEqual((second.returncode, second.stdout), (first.returncode, first.stdout))
        self.assertEqual((dry.returncode, dry.stdout), (first.returncode, first.stdout))
        self.assertEqual({p.name: p.read_bytes() for p in self.inputs.iterdir()}, before)
        self.assertEqual(first.stderr + second.stderr + dry.stderr, "")

    def test_help_is_offline_and_discovers_machine_replay_and_checks(self):
        result = self.invoke(["--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        for term in ("--jobs", "--json", "--dry-run", "--min-attempts", "--min-hours"):
            self.assertIn(term, result.stdout)
        self.assertIn("test_capacity_evidence_contract", result.stdout)

    def test_duplicate_json_completeness_key_is_refused_before_last_value_wins(self):
        # A normal JSON decoder would silently replace false with true.
        text = json.dumps(sample())
        self.assertEqual(text.count('"complete": true'), 1)
        text = text.replace('"complete": true', '"complete": false, "complete": true', 1)
        fixture = self.inputs / "duplicate-completeness.json"
        fixture.write_text(text)
        before = fixture.read_bytes()
        result = self.invoke(["--jobs", str(fixture), "--json"])
        self.wire(result, "ERROR", "INVALID_JOBS", exit_code=2)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual(fixture.read_bytes(), before)

    def test_in_progress_status_alone_excludes_otherwise_finished_intervals(self):
        data = sample()
        for row in data["jobs"]:
            row["status"] = "completed"
            if row["runner_name"].startswith("coolify") and row["id"] != 109:
                # Keep valid clocks AND a terminal conclusion: only status is bad.
                row["status"] = "in_progress"
        body = self.wire(self.report(data, machine=True), "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE")
        self.assertEqual(body["counts"]["input_rows"], 20)
        self.assertEqual(body["counts"]["excluded_clock"], 9)
        self.assertEqual(body["counts"]["malformed_rows"], 0)
        self.assertEqual(body["counts"]["accepted_rows"], 11)
        self.assertEqual(body["arms"]["coolify"]["matched_attempts"], 1)
        self.assertEqual(body["arms"]["garm"]["matched_attempts"], 10)
        self.assertIs(body["evidence_complete"], False)
        self.assertIs(body["capacity"]["residual_headroom_known"], False)
        self.assertIsNone(body["capacity"]["combined_peak"])

    def test_repeated_value_flags_refuse_even_when_last_value_would_allow_go(self):
        data = sample(attempts=9)
        # Prove that last-value-wins could weaken an actual adequacy boundary.
        self.wire(self.report(data, "--min-attempts", "10", machine=True),
                  "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE")
        self.wire(self.report(data, "--min-attempts", "9", machine=True),
                  "GO", "MEASURED_FIT")
        first = self.inputs / "first-valid.json"
        second = self.inputs / "second-valid.json"
        for fixture in (first, second):
            fixture.write_text(json.dumps(data))
        before = {p: p.read_bytes() for p in (first, second)}
        values = {
            "--jobs": (str(first), str(second)),
            "--node7b1ff338-max": ("2", "3"),
            "--node575e13db-max": ("4", "5"),
            "--min-hours": ("24", "25"),
            "--min-attempts": ("10", "9"),
        }
        for flag in VALUE_FLAGS:
            with self.subTest(flag=flag):
                left, right = values[flag]
                args = [] if flag == "--jobs" else ["--jobs", str(first)]
                result = self.invoke([*args, flag, left, flag, right, "--json"])
                self.wire(result, "ERROR", "INVALID_OPTION_VALUE", exit_code=2)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual({p: p.read_bytes() for p in (first, second)}, before)

    def test_idless_copy_of_identified_interval_is_a_duplicate_not_new_compute(self):
        data = sample()
        repeated = copy.deepcopy(data["jobs"][0])  # job 100, not a repeated ID
        repeated.pop("id")
        data["jobs"].append(repeated)
        body = self.wire(self.report(data, machine=True), "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE")
        self.assertEqual(body["counts"]["input_rows"], 21)
        self.assertEqual(body["counts"]["duplicate_rows"], 1)
        self.assertEqual(body["counts"]["accepted_rows"], 20)
        self.assertEqual(body["counts"]["malformed_rows"], 0)
        for arm in ("coolify", "garm"):
            self.assertEqual(body["arms"][arm]["matched_attempts"], 10)
        self.assertEqual(body["job_ids"].count(100), 1)
        self.assertIs(body["evidence_complete"], False)
        self.assertIs(body["capacity"]["residual_headroom_known"], False)
        self.assertIsNone(body["capacity"]["combined_peak"])

    def test_truthy_non_boolean_completeness_does_not_assert_literal_true(self):
        for value in (1, "true"):
            with self.subTest(complete=value):
                data = sample()
                data["evidence"]["complete"] = value
                body = self.wire(self.report(data, machine=True), "INCONCLUSIVE", "INSUFFICIENT_EVIDENCE")
                self.assertEqual(body["counts"]["accepted_rows"], 20)
                for arm in ("coolify", "garm"):
                    self.assertEqual(body["arms"][arm]["matched_attempts"], 10)
                    self.assertIs(body["arms"][arm]["adequate"], True)
                self.assertIs(body["evidence_complete"], False)
                self.assertIsNone(body["capacity"]["combined_peak"])
                self.assertIs(body["capacity"]["residual_headroom_known"], False)


if __name__ == "__main__":
    unittest.main()
