"""Independent, offline CLI acceptance for the alert-only sync-back observer.

Run: python3 -B -m unittest -v test_workspace_sync_back_signal_acceptance

Expectations come from the measurement/alert contract, not implementation
helpers. Only the HTTP boundary is replaced: the real wrapper, classifier and
shared Gatus helper run with synthetic configuration. Child-only Linux Landlock
confines writes to this fixture; seccomp denies sockets, including absolute
network commands. An allowlisted PATH records forbidden transport/host calls.
Missing containment is an error, never permission to run uncontained.

Breaks caught: decimal/binary and strict-greater-than drift; false healthy unknowns;
Python bool/int coercion; permissive JSON/Unicode parsing; character-vs-byte caps;
input/credential disclosure; alternate sink/endpoint or success inversion; treating
classification as a hard stop; swallowing or leaking delivery failures.
No production runtime, alert activation, transport or rollout is qualified here.
"""

import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent
CLASSIFIER = ROOT / "ops/paperclip/workspace_sync_back_signal.py"
WRAPPER = ROOT / "ops/paperclip/push-workspace-sync-back.sh"
HELPER = ROOT / "ops/case_658bf6b9/_gatus_push.sh"
OBSERVATION = {
    "schema_version": 1,
    "measurement": "sync_back_payload_bytes",
    "complete": True,
    "bytes": 2_000_000_000,
}
MARKER = "SYNTHETIC_PRIVATE_INPUT_91873"
TOKEN = "SYNTHETIC_GATUS_TOKEN_64127"
SINK_ERROR = "SYNTHETIC_PRIVATE_DELIVERY_85219"

# This adapter records the real helper's HTTP request, not a fabricated verdict.
CURL_STUB = r'''#!/usr/bin/python3
import json, os, pathlib, stat, sys
args = sys.argv[1:]
record = {"argv": args}
if "--config" in args:
    path = pathlib.Path(args[args.index("--config") + 1])
    record["config_mode"] = stat.S_IMODE(path.stat().st_mode)
    record["config_path"] = str(path)
    record["config"] = path.read_text()
with open(os.environ["ACCEPTANCE_CURL_LOG"], "a") as f:
    f.write(json.dumps(record) + "\n")
code = int(os.environ.get("ACCEPTANCE_CURL_EXIT", "0"))
if code:
    sys.stderr.write("SYNTHETIC_PRIVATE_DELIVERY_85219\n")
    sys.exit(code)
print(os.environ.get("ACCEPTANCE_HTTP_STATUS", "204"), end="")
'''

DENIED_STUB = r'''#!/usr/bin/python3
import json, os, pathlib, sys
with open(os.environ["ACCEPTANCE_DENIED_LOG"], "a") as f:
    f.write(json.dumps({"command": pathlib.Path(sys.argv[0]).name, "args": sys.argv[1:]}) + "\n")
sys.exit(97)
'''

# Observe function execution in the *real* shared helper without replacing it.
BASH_TRACE = r'''_acceptance_trace() {
  if [[ -n "${BASH_SOURCE[1]:-}" && "${BASH_SOURCE[1]}" -ef "$ACCEPTANCE_HELPER" ]]; then
    printf '%s\n' "${FUNCNAME[1]:-source}" >> "$ACCEPTANCE_HELPER_LOG"
  fi
}
set -T
trap '_acceptance_trace' DEBUG
command_not_found_handle() {
  printf '%s\n' 'unlisted command' >> "$ACCEPTANCE_DENIED_LOG"
  return 97
}
'''


def confine_child(writable, executables):
    """Fail-closed Linux fences applied after fork, before the real CLI exec."""
    libc = ctypes.CDLL(None, use_errno=True)

    def checked(result, label):
        if result < 0:
            raise OSError(ctypes.get_errno(), label)
        return result

    checked(libc.prctl(38, 1, 0, 0, 0), "no_new_privs")
    abi = checked(libc.syscall(444, 0, 0, 1), "Landlock ABI")
    if abi < 3:
        raise RuntimeError("Landlock ABI >= 3 required")
    writes = (1 << 1) | sum(1 << bit for bit in range(4, 15))
    execute = 1
    attr = ctypes.c_uint64(writes | execute)
    ruleset = checked(libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0), "Landlock create")

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    grants = [(writable, writes | execute), ("/dev/null", (1 << 1) | (1 << 14))]
    grants.extend((path, execute) for path in executables)
    for path, access in grants:
        fd = os.open(path, os.O_PATH)
        try:
            rule = PathRule(access, fd)
            checked(libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0), "Landlock grant")
        finally:
            os.close(fd)
    checked(libc.syscall(446, ruleset, 0), "Landlock restrict")
    os.close(ruleset)

    numbers = {"x86_64": (41, 42, 53), "aarch64": (198, 199, 203)}
    if platform.machine() not in numbers:
        raise RuntimeError("Unsupported socket-filter architecture")

    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                    ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]

    class Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]

    instructions = [Filter(0x20, 0, 0, 0)]
    for number in numbers[platform.machine()]:
        instructions.extend([Filter(0x15, 0, 1, number),
                             Filter(0x06, 0, 0, 0x50000 | errno.EPERM)])
    instructions.append(Filter(0x06, 0, 0, 0x7FFF0000))
    filters = (Filter * len(instructions))(*instructions)
    program = Program(len(filters), filters)
    checked(libc.prctl(22, 2, ctypes.byref(program), 0, 0), "seccomp sockets")


def encoded(**changes):
    return json.dumps(dict(OBSERVATION, **changes), separators=(",", ":")).encode()


class WorkspaceSyncBackAcceptance(unittest.TestCase):
    def setUp(self):
        scratch = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("PAPERCLIP_SCRATCH_DIR")
        self.temp = tempfile.TemporaryDirectory(prefix="sync-back-acceptance-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.root / "tmp").mkdir()
        (self.root / "home").mkdir()
        self.curl_log = self.root / "curl.jsonl"
        self.denied_log = self.root / "denied.jsonl"
        self.helper_log = self.root / "helper.log"
        for path in (self.curl_log, self.denied_log, self.helper_log):
            path.write_text("")
        (self.root / "trace.sh").write_text(BASH_TRACE)
        self.allowed = []
        for name in ("bash", "python3", "dirname", "mktemp", "chmod", "rm", "cat"):
            actual = shutil.which(name, path="/usr/bin:/bin")
            self.assertIsNotNone(actual, f"Required fixture tool: {name}")
            actual = str(Path(actual).resolve())
            self.allowed.append(actual)
            (self.bin / name).symlink_to(actual)
        # ELF interpreters need an execute grant as well as their binaries.
        for loader in ("/lib64/ld-linux-x86-64.so.2", "/lib/ld-linux-aarch64.so.1"):
            if Path(loader).exists():
                self.allowed.append(str(Path(loader).resolve()))
        self.script("curl", CURL_STUB)
        for name in ("ssh", "scp", "sftp", "rsync", "wget", "nc", "socat", "git", "gh",
                     "docker", "podman", "systemctl", "sudo", "psql", "find", "du", "cp", "mv"):
            self.script(name, DENIED_STUB)
        self.env = {
            "PATH": str(self.bin), "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"), "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
            "GATUS_URL": "https://sink.invalid", "GATUS_EXTERNAL_TOKEN": TOKEN,
            "ACCEPTANCE_CURL_LOG": str(self.curl_log),
            "ACCEPTANCE_DENIED_LOG": str(self.denied_log),
            "ACCEPTANCE_HELPER_LOG": str(self.helper_log),
            "ACCEPTANCE_HELPER": str(HELPER),
            "BASH_ENV": str(self.root / "trace.sh"),
        }

    def script(self, name, content):
        path = self.bin / name
        # Replacing a fixture symlink must never overwrite its real executable.
        path.unlink(missing_ok=True)
        path.write_text(content)
        path.chmod(0o755)

    def invoke(self, payload, wrapper=False, stdin=None, **environment):
        self.curl_log.write_text("")
        self.denied_log.write_text("")
        self.helper_log.write_text("")
        env = dict(self.env, **environment)
        command = [str(self.bin / "bash"), str(WRAPPER)] if wrapper else [str(self.bin / "python3"), str(CLASSIFIER)]
        input_options = {"input": payload} if stdin is None else {"stdin": stdin}
        result = subprocess.run(
            command, capture_output=True, cwd=self.root, env=env, timeout=5,
            preexec_fn=lambda: confine_child(str(self.root), self.allowed), **input_options,
        )
        self.assertEqual(self.denied_log.read_text(), "", "Observer attempted a transport/host operation")
        for marker in (MARKER, TOKEN, SINK_ERROR):
            self.assertNotIn(marker.encode(), result.stdout + result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertLess(len(result.stdout), 4096)
        self.assertLess(len(result.stderr), 1024)
        return result

    def report(self, result):
        try:
            report = json.loads(result.stdout)
        except (ValueError, UnicodeError) as error:
            self.fail(f"CLI must emit one JSON report: {result!r}; {error}")
        self.assertIsInstance(report, dict)
        self.assertEqual(report["schema"], "workspace-sync-back-signal.v1")
        self.assertIs(report["alert_only"], True)
        self.assertIs(report["blocks_execution"], False)
        self.assertEqual(report["threshold_bytes"], 2_000_000_000)
        self.assertEqual(report["unit"], "bytes")
        self.assertEqual(report["gatus"]["endpoint_key"], "platform_workspace-sync-back")
        self.assertEqual(report["gatus"]["error"],
                         report["message"] if report["alert_required"] else "")
        self.assertIsInstance(report["reason"], str)
        self.assertTrue(report["reason"])
        self.assertIsInstance(report["message"], str)
        self.assertTrue(report["message"])
        self.assertLessEqual(len(report["message"]), 500)
        self.assertNotRegex(report["message"], r"[\x00-\x1f\x7f]")
        return report

    def classification(self, payload, status, count, stdin=None):
        result = self.invoke(payload, stdin=stdin)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stderr, b"")
        report = self.report(result)
        self.assertEqual(report["status"], status)
        self.assertEqual(report["bytes"], count)
        self.assertIs(report["measurement_qualified"], status != "unknown")
        self.assertIs(report["alert_required"], status != "within_threshold")
        self.assertIs(report["gatus"]["success"], status == "within_threshold")
        if count is not None:
            self.assertIs(type(report["bytes"]), int)
        self.assertEqual(self.curl_log.read_text(), "", "Classifier must never deliver alerts")
        return report

    def unknown(self, payload):
        return self.classification(payload, "unknown", None)

    def requests(self):
        return [json.loads(line) for line in self.curl_log.read_text().splitlines()]

    def delivery(self, payload, status, count, **environment):
        result = self.invoke(payload, wrapper=True, **environment)
        report = self.report(result)
        self.assertEqual(report["status"], status)
        self.assertEqual(report["bytes"], count)
        self.assertIs(report["measurement_qualified"], status != "unknown")
        self.assertIs(report["alert_required"], status != "within_threshold")
        self.assertIs(report["gatus"]["success"], status == "within_threshold")
        requests = self.requests()
        self.assertEqual(len(requests), 1, "Exactly one request, no retries")
        request = requests[0]
        self.assertNotIn(TOKEN, " ".join(request["argv"]))
        self.assertIn("--config", request["argv"])
        self.assertEqual(request["config_mode"], 0o600)
        config = {}
        for line in request["config"].splitlines():
            if " = " in line:
                key, value = line.split(" = ", 1)
                config[key] = json.loads(value)
        self.assertEqual(config["request"], "POST")
        self.assertEqual(config["header"], "Authorization: Bearer " + TOKEN)
        url = urlsplit(config["url"])
        self.assertEqual((url.scheme, url.netloc, url.path, url.fragment),
                         ("https", "sink.invalid", "/api/v1/endpoints/platform_workspace-sync-back/external", ""))
        query = parse_qs(url.query, keep_blank_values=True)
        errors = query.pop("error", [])
        self.assertEqual(query, {"success": ["true" if status == "within_threshold" else "false"]})
        if status == "within_threshold":
            # A recovery needs success=true; healthy prose is optional.
            self.assertIn(errors, ([], [""], [report["message"]]))
        else:
            self.assertEqual(errors, [report["message"]])
        self.assertIn("gatus_push", self.helper_log.read_text().splitlines(),
                      "Must execute the existing shared helper, not an alternate HTTP path")
        self.assertFalse(Path(request["config_path"]).exists(), "Synthetic credential file must be disposed")
        self.assertEqual(list((self.root / "tmp").iterdir()), [])
        self.assertNotIn(MARKER, request["config"])
        return result, report

    def test_zero_is_a_qualified_size_not_unknown(self):
        self.classification(encoded(bytes=0), "within_threshold", 0)

    def test_below_decimal_threshold(self):
        self.classification(encoded(bytes=1_999_999_999), "within_threshold", 1_999_999_999)

    def test_equality_is_not_oversized(self):
        self.classification(encoded(), "within_threshold", 2_000_000_000)

    def test_first_byte_above_decimal_threshold_alerts(self):
        self.classification(encoded(bytes=2_000_000_001), "oversized", 2_000_000_001)

    def test_two_gib_is_above_decimal_two_gb(self):
        self.classification(encoded(bytes=2_147_483_648), "oversized", 2_147_483_648)

    def test_supported_exact_integer_maximum(self):
        self.classification(encoded(bytes=9_007_199_254_740_991), "oversized", 9_007_199_254_740_991)

    def test_count_range_rejects_negative_and_above_max(self):
        for count in (-1, -2_000_000_001, 9_007_199_254_740_992, 10**40):
            with self.subTest(count=count):
                self.unknown(encoded(bytes=count))

    def test_count_rejects_bool_float_string_null_and_containers(self):
        for count in (True, False, 0.0, 2_000_000_000.0, "0", "2000000001", None, [], {}):
            with self.subTest(count=count):
                self.unknown(encoded(bytes=count))

    def test_exponent_syntax_is_not_an_integer_count(self):
        self.unknown(encoded().replace(b'2000000000', b'2e9'))

    def test_each_required_field_must_exist(self):
        for field in OBSERVATION:
            with self.subTest(field=field):
                observation = dict(OBSERVATION)
                del observation[field]
                self.unknown(json.dumps(observation).encode())

    def test_schema_must_be_integer_one_not_coercible(self):
        for value in (True, False, 1.0, "1", 0, 2, None, [], {}):
            with self.subTest(value=value):
                self.unknown(encoded(schema_version=value))

    def test_complete_must_be_boolean_true(self):
        for value in (False, 1, 0, 1.0, "true", None, [], {}):
            with self.subTest(value=value):
                self.unknown(encoded(complete=value))

    def test_partial_count_above_threshold_is_still_unknown_not_oversized(self):
        self.unknown(encoded(complete=False, bytes=2_000_000_001))

    def test_measurement_must_be_exact_payload_bytes(self):
        for value in ("run_folder_bytes", "allocated_bytes", "sync_back_payload_bytes ",
                      "SYNC_BACK_PAYLOAD_BYTES", "", True, 1, None, [], {}):
            with self.subTest(value=value):
                self.unknown(encoded(measurement=value))

    def test_non_object_and_malformed_json_never_crash_or_score_healthy(self):
        for raw in (b"", b" \n\t", b"null", b"[]", b"true", b"123", b'"text"',
                    b"{", b"{bad}", encoded()[:-1], encoded() + b"{}",
                    encoded() + b"\x00", encoded().replace(b"2000000000", b"02000000000")):
            with self.subTest(raw=raw):
                self.unknown(raw)

    def test_duplicate_required_keys_rejected_even_when_values_agree(self):
        for field, value in OBSERVATION.items():
            with self.subTest(field=field):
                suffix = json.dumps(field).encode() + b":" + json.dumps(value).encode()
                self.unknown(encoded()[:-1] + b"," + suffix + b"}")

    def test_duplicate_keys_cannot_hide_contradictory_counts_or_completeness(self):
        for suffix in (b'"bytes":2000000001', b'"bytes":0', b'"complete":false'):
            with self.subTest(suffix=suffix):
                self.unknown(encoded()[:-1] + b"," + suffix + b"}")

    def test_duplicate_keys_in_ignored_nested_objects_are_invalid_json(self):
        self.unknown(encoded()[:-1] + b',"ignored":{"x":1,"x":1}}')

    def test_nonfinite_values_invalid_even_in_ignored_fields(self):
        for number in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(number=number):
                self.unknown(encoded().replace(b"2000000000", number))
                self.unknown(encoded()[:-1] + b',"ignored":' + number + b"}")

    def test_invalid_utf8_never_crashes_or_discloses_input(self):
        for invalid in (b"\xff", b"\xc0\xaf", b"\xed\xa0\x80", b"\xf4\x90\x80\x80"):
            with self.subTest(invalid=invalid):
                self.unknown(encoded()[:-1] + b',"ignored":"' + invalid + b'"}')

    def test_lone_surrogate_escapes_are_not_valid_unicode_observations(self):
        for invalid in (b"\\ud800", b"\\udfff", b"\\ud800x", b"\\udfff\\ud800"):
            with self.subTest(invalid=invalid):
                self.unknown(encoded()[:-1] + b',"ignored":"' + invalid + b'"}')

    def test_invalid_unicode_in_ignored_keys_and_nested_values_is_unknown(self):
        for suffix in (b',"\\ud800":0}', b',"ignored":{"\\udfff":1}}',
                       b',"ignored":[{"nested":"\\ud800"}]}'):
            with self.subTest(suffix=suffix):
                self.unknown(encoded()[:-1] + suffix)

    def test_valid_unicode_and_ignored_fields_do_not_change_the_report(self):
        expected = self.classification(encoded(), "within_threshold", 2_000_000_000)
        extra = {"ignored": {"private_path": "/synthetic/" + MARKER, "token": MARKER,
                             "unicode": "é\U0001f680", "nested": [1, False, None]},
                 "endpoint": MARKER, "success": False, "status": MARKER,
                 "alert_only": False, "blocks_execution": True}
        self.assertEqual(self.classification(encoded(**extra), "within_threshold", 2_000_000_000), expected)
        escaped_pair = encoded()[:-1] + b',"ignored":"\\ud83d\\ude80"}'
        self.assertEqual(self.classification(escaped_pair, "within_threshold", 2_000_000_000), expected)

    def test_exact_4096_byte_input_is_accepted_but_next_byte_is_unknown(self):
        raw = encoded()
        at_cap = raw + b" " * (4096 - len(raw))
        self.assertEqual(len(at_cap), 4096)
        self.classification(at_cap, "within_threshold", 2_000_000_000)
        self.unknown(at_cap + b" ")

    def test_input_cap_counts_utf8_bytes_not_characters(self):
        base = encoded(ignored="é" * 1950).decode().replace("\\u00e9", "é").encode()
        self.assertLess(len(base), 4096)
        at_cap = base + b" " * (4096 - len(base))
        self.classification(at_cap, "within_threshold", 2_000_000_000)
        self.unknown(at_cap + b" ")

    def test_unreadable_stdin_is_unknown_and_exits_zero(self):
        # Write-only fd initializes Python normally, then the real read gets EBADF.
        fd = os.open(self.root / "unreadable-stdin", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            self.classification(b"", "unknown", None, stdin=fd)
        finally:
            os.close(fd)

    def test_large_but_bounded_input_is_unknown(self):
        self.unknown(encoded(ignored=MARKER * 300))

    def test_deep_non_observation_json_still_exits_zero(self):
        self.unknown(b"[" * 1100 + b"0" + b"]" * 1100)

    def test_malformed_input_does_not_disclose_markers_or_parser_exceptions(self):
        first = self.unknown(b'{"' + MARKER.encode() + b'":')
        second = self.unknown(b'{"ANOTHER_SYNTHETIC_INPUT":')
        self.assertEqual(first, second)

    def test_wrapper_delivers_below_equal_and_zero_as_success(self):
        for count in (0, 1_999_999_999, 2_000_000_000):
            with self.subTest(count=count):
                result, _ = self.delivery(encoded(bytes=count), "within_threshold", count)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrapper_delivers_above_binary_and_max_as_failure_without_hard_stop(self):
        for count in (2_000_000_001, 2_147_483_648, 9_007_199_254_740_991):
            with self.subTest(count=count):
                result, _ = self.delivery(encoded(bytes=count), "oversized", count)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrapper_delivers_unknowns_as_failure_without_hard_stop(self):
        for payload in (b"", b'{"' + MARKER.encode() + b'":', encoded(complete=False),
                        encoded(schema_version=True), encoded(bytes=True),
                        encoded()[:-1] + b',"bytes":0}',
                        encoded(ignored=MARKER * 300),
                        encoded()[:-1] + b',"ignored":"\xff"}'):
            with self.subTest(payload=payload[:150]):
                result, _ = self.delivery(payload, "unknown", None)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrapper_invalid_unicode_alerts_instead_of_false_recovery(self):
        for invalid in (b"\xed\xa0\x80", b"\\ud800", b"\\udfff"):
            with self.subTest(invalid=invalid):
                payload = encoded()[:-1] + b',"ignored":"' + invalid + b'"}'
                result, _ = self.delivery(payload, "unknown", None)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrapper_unreadable_input_delivers_unknown_without_hard_stop(self):
        # Write-only fd initializes Python normally, then the real read gets EBADF.
        fd = os.open(self.root / "unreadable-stdin", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            result, _ = self.delivery(b"", "unknown", None, stdin=fd)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.close(fd)

    def test_observer_failure_is_nonzero_without_delivery_or_raw_diagnostics(self):
        # Fail at the interpreter boundary; never alter the real classifier.
        self.script("python3", '''#!/usr/bin/python3
import sys
print("SYNTHETIC_PRIVATE_INPUT_91873")
sys.stderr.write("SYNTHETIC_PRIVATE_DELIVERY_85219\\n")
sys.exit(70)
''')
        result = self.invoke(encoded(), wrapper=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.stderr)
        self.assertEqual(self.requests(), [])

    def test_wrapper_ignored_fields_never_reach_sink(self):
        result, _ = self.delivery(encoded(ignored=MARKER, endpoint=MARKER, url=MARKER),
                                  "within_threshold", 2_000_000_000)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_http_failures_are_delivery_failures_not_reclassification(self):
        for http in ("302", "401", "500"):
            with self.subTest(http=http):
                result, _ = self.delivery(encoded(), "within_threshold", 2_000_000_000,
                                          ACCEPTANCE_HTTP_STATUS=http)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr)

    def test_transport_failure_is_nonzero_sanitized_and_not_retried(self):
        for payload, status, count in ((encoded(), "within_threshold", 2_000_000_000),
                                       (encoded(bytes=2_000_000_001), "oversized", 2_000_000_001),
                                       (b"{", "unknown", None)):
            with self.subTest(status=status):
                result, _ = self.delivery(payload, status, count, ACCEPTANCE_CURL_EXIT="7")
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr)

    def test_missing_sink_configuration_is_nonzero_without_sending(self):
        for name in ("GATUS_URL", "GATUS_EXTERNAL_TOKEN"):
            with self.subTest(name=name):
                result = self.invoke(encoded(), wrapper=True, **{name: ""})
                report = self.report(result)
                self.assertEqual(report["status"], "within_threshold")
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr)
                self.assertEqual(self.requests(), [])


if __name__ == "__main__":
    unittest.main()
