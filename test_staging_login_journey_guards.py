"""Guard-regression suite for the staging-login probe (builder-owned).

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_staging_login_journey_guards

Companion to the immutable Test Author contract in
test_staging_login_journey.py, which this file imports but never modifies.
It pins behaviors that contract intentionally leaves open: cookie attribute
edge cases, duplicate response evidence, the ambient curl-config guard,
abort/push-failure outcomes and response size bounds.

The adapter is derived from the immutable ADAPTER string with exactly two
deltas: curl argv recording (to pin -q-first on every staging call) and a
blocking mode (so signal tests can interrupt a known step). Every patch
anchor is verified at import, and each delta is self-validating: its tests
fail if the delta is absent.
"""

import ctypes
import errno
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from test_staging_login_journey import (
    ADAPTER as BASE_ADAPTER,
    ENTRYPOINT,
    ORIGIN,
    PROBE_ID,
    READ_TOOLS,
    ROOT,
    STEP_IDS,
)


def _derive_adapter():
    adapter = BASE_ADAPTER
    patches = [
        # Blocking mode needs a clock.
        ("import json, os, pathlib, re, shlex, stat, subprocess, sys, urllib.parse",
         "import json, os, pathlib, re, shlex, stat, subprocess, sys, time, urllib.parse"),
        # Record curl argv on staging calls so tests can pin -q-first.
        # The adapter already refuses any argv carrying a credential or
        # cookie value, so recording argv cannot leak fixture secrets.
        ('    record(kind="http", step=step, attempt=attempt + 1 if step == "login" else 1,\n',
         '    record(kind="http", argv=args, step=step, attempt=attempt + 1 if step == "login" else 1,\n'),
        # Blocking mode: the scenario names one step whose curl call sleeps
        # instead of returning, so the test can deliver a signal mid-step.
        ('    sys.stdout.write(str(code) + " " + str(seconds))\n',
         '    if scenario.get("block_step") == step:\n'
         '        record(kind="blocked", step=step)\n'
         '        time.sleep(30)\n'
         '    sys.stdout.write(str(code) + " " + str(seconds))\n'),
    ]
    for old, new in patches:
        if adapter.count(old) != 1:
            raise RuntimeError("guard adapter anchor changed; refusing silent drift: %r" % old[:60])
        adapter = adapter.replace(old, new, 1)
    return adapter


ADAPTER = _derive_adapter()

OVERSIZE = 70000  # Above the probe's 65536-byte streaming/parse bound.


class StagingLoginJourneyGuardTests(unittest.TestCase):
    """Pin probe guards the acceptance contract leaves open."""

    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash")
        if not cls.bash:
            raise RuntimeError("offline suite requires bash")
        cls.tools = {name: shutil.which(name) for name in (*READ_TOOLS, "mktemp", "chmod", "rm")}
        cls.tools = {name: path for name, path in cls.tools.items() if path}
        for required in ("python3", "dirname", "grep", "tr", "mktemp", "chmod", "rm"):
            if required not in cls.tools:
                raise RuntimeError("offline suite missing local tool: " + required)

        # Same network fence as the acceptance contract: PATH stubs alone
        # cannot contain an absolute-path curl command.
        cls.seccomp = ctypes.CDLL("libseccomp.so.2")
        cls.seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
        cls.seccomp.seccomp_init.restype = ctypes.c_void_p
        cls.seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
        cls.seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        cls.seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
        cls.seccomp.seccomp_release.argtypes = [ctypes.c_void_p]

    def deny_network(self):
        context = self.seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
        if not context:
            os._exit(98)
        for name in (b"socket", b"connect", b"sendto", b"sendmsg"):
            syscall = self.seccomp.seccomp_syscall_resolve_name(name)
            if syscall < 0 or self.seccomp.seccomp_rule_add(context, 0x00050000 | errno.EPERM, syscall, 0) != 0:
                os._exit(98)
        if self.seccomp.seccomp_load(context) != 0:
            os._exit(98)
        self.seccomp.seccomp_release(context)

    def run_probe(self, scenario=None, flags=(), env_overrides=None,
                  block_step=None, deliver_signal=None):
        parent = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("PAPERCLIP_SCRATCH_DIR") or os.environ.get("TMPDIR")
        with tempfile.TemporaryDirectory(prefix="staging-journey-guards-", dir=parent) as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "tmp").mkdir(mode=0o700)
            (root / "home").mkdir(mode=0o700)
            (root / "events.jsonl").touch()
            qa, gatus, cookie, old_cookie = (secrets.token_hex(24) for _ in range(4))
            values = {"qa": qa, "gatus": gatus, "cookie": cookie, "old_cookie": old_cookie}
            resolved = scenario(values) if callable(scenario) else (scenario or {})
            if block_step is not None:
                resolved = dict(resolved)
                resolved["block_step"] = block_step
            (root / "scenario.json").write_text(json.dumps(resolved))
            adapter = root / "adapter"
            adapter.write_text("#!" + sys.executable + "\n" + ADAPTER)
            adapter.chmod(0o700)
            for name in (*self.tools, "curl", "sleep"):
                (root / "bin" / name).symlink_to(adapter)
            # No /usr/bin or /bin fallback: an unrecognized external command
            # terminates the journey instead of accidentally reaching the host.
            guard = root / "guard.sh"
            guard.write_text('''\
command_not_found_handle() { printf 'offline fixture unknown command\\n' >&2; exit 97; }
__offline_guard() {
    case "$1" in
        /*|./*|../*|exec\\ /*|command\\ /*)
            printf 'offline fixture absolute command refused\\n' >&2
            exit 97 ;;
    esac
}
set -o functrace
trap '__offline_guard "$BASH_COMMAND"' DEBUG
''')
            env = {
                "PATH": str(root / "bin"), "HOME": str(root / "home"),
                "TMPDIR": str(root / "tmp"), "BASH_ENV": str(guard), "LC_ALL": "C",
                "PYTHONDONTWRITEBYTECODE": "1", "FIXTURE_ROOT": str(root),
                "FIXTURE_TOOLS": json.dumps(self.tools), "FIXTURE_QA": qa,
                "FIXTURE_GATUS": gatus, "FIXTURE_COOKIE": cookie,
                "FIXTURE_OLD_COOKIE": old_cookie, "QA_AUTH_TOKEN": qa,
                "GATUS_EXTERNAL_TOKEN": gatus, "GATUS_URL": "https://gatus.invalid",
            }
            env.update(env_overrides or {})
            process = subprocess.Popen([self.bash, str(ENTRYPOINT), *flags], cwd=ROOT,
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, start_new_session=True, preexec_fn=self.deny_network)
            if deliver_signal is not None:
                self._await_block(root, block_step)
                os.killpg(process.pid, deliver_signal)
            try:
                stdout, stderr = process.communicate(timeout=12)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                self.fail("entrypoint exceeded the 12-second offline execution bound")
            events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
            # Check redaction without ever including a value in assertion output.
            self.assertFalse(any(v in stdout + stderr for v in (qa, gatus, cookie, old_cookie)),
                             "credential/cookie disclosed by the real entrypoint")
            violations = [e for e in events if e["kind"] == "violation"]
            self.assertEqual(violations, [], "unexpected network/host operation (no request was sent)")
            self.assertNotEqual(process.returncode, 98, "could not install the offline network fence")
            self.assertNotIn("offline fixture unknown command", stderr,
                             "entrypoint attempted an unallowlisted host command")
            self.assertNotIn("offline fixture absolute command refused", stderr,
                             "entrypoint attempted an absolute-path host command")
            self.assertEqual(list((root / "tmp").iterdir()), [], "entrypoint leaked private temporary state")
            return {"rc": process.returncode, "stdout": stdout, "stderr": stderr, "events": events}

    def _await_block(self, root, step):
        deadline = time.time() + 10
        while time.time() < deadline:
            for line in (root / "events.jsonl").read_text().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("kind") == "blocked" and event.get("step") == step:
                    return
            time.sleep(0.05)
        self.fail("probe did not reach the blocking step: " + str(step))

    def http(self, result, step=None):
        return [e for e in result["events"] if e["kind"] == "http" and (step is None or e["step"] == step)]

    def assert_push(self, result, success):
        self.assertEqual([e["success"] for e in result["events"] if e["kind"] == "gatus"], [success])

    def failed_document(self, result, code):
        try:
            document = json.loads(result["stdout"])
        except (ValueError, TypeError):
            self.fail("--json must emit exactly one JSON result on failure")
        self.assertEqual(document.get("schema_version"), 1)
        self.assertEqual(document.get("probe_id"), PROBE_ID)
        self.assertEqual(document.get("outcome"), "failed")
        self.assertIsInstance(document.get("steps"), list)
        error = document.get("error")
        self.assertIsInstance(error, dict)
        self.assertEqual(error.get("code"), code)
        self.assertIs(type(error.get("retryable")), bool)
        self.assertIsInstance(error.get("next_action"), str)
        self.assertTrue(error["next_action"].strip())
        self.assertNotIn("DO_NOT_DUMP", json.dumps(document))
        return document

    def cookie_result(self, *lines):
        return self.run_probe(lambda v: {"login": {"cookie": "none", "headers": [
            line.replace("<COOKIE>", v["cookie"]) for line in lines]}})

    def assert_login_rejected(self, result):
        self.assertEqual(result["rc"], 1)
        self.assertEqual([e["step"] for e in self.http(result)], ["login"])
        self.assert_push(result, False)

    # --- cookie attribute edges ---

    def test_spaced_domain_attribute_is_rejected(self):
        for tail in ("Domain =togetherweown.com", "Domain = togetherweown.com",
                     "\tDomain\t=\ttogetherweown.com", " Domain=togetherweown.com "):
            with self.subTest(tail=tail):
                self.assert_login_rejected(self.cookie_result(
                    "Set-Cookie: __Host-two_session=<COOKIE>; Path=/; Secure; HttpOnly; " + tail))

    def test_plain_and_bare_domain_are_rejected(self):
        for tail in ("Domain=togetherweown.com", "Domain", "domain=x"):
            with self.subTest(tail=tail):
                self.assert_login_rejected(self.cookie_result(
                    "Set-Cookie: __Host-two_session=<COOKIE>; Path=/; Secure; HttpOnly; " + tail))

    def test_duplicate_path_is_rejected_in_both_orders(self):
        for extra in ("Path=/; Path=/auth", "Path=/auth; Path=/"):
            with self.subTest(extra=extra):
                self.assert_login_rejected(self.cookie_result(
                    "Set-Cookie: __Host-two_session=<COOKIE>; " + extra + "; Secure; HttpOnly"))

    def test_wrong_and_missing_path_are_rejected(self):
        for attrs in ("Path=/other; Secure; HttpOnly", "Secure; HttpOnly", "Path; Secure; HttpOnly"):
            with self.subTest(attrs=attrs):
                self.assert_login_rejected(self.cookie_result(
                    "Set-Cookie: __Host-two_session=<COOKIE>; " + attrs))

    def test_empty_and_missing_cookie_value_are_rejected(self):
        for record in ("__Host-two_session=; Path=/; Secure; HttpOnly",
                       "__Host-two_session; Path=/; Secure; HttpOnly",
                       "__Host-two_session =<COOKIE>; Path=/; Secure; HttpOnly"):
            with self.subTest(record=record):
                self.assert_login_rejected(self.cookie_result("Set-Cookie: " + record))

    def test_duplicate_session_records_are_rejected(self):
        good = "Set-Cookie: __Host-two_session=<COOKIE>; Path=/; Secure; HttpOnly"
        bad = "Set-Cookie: __Host-two_session=<COOKIE>; Path=/other; Secure; HttpOnly"
        self.assert_login_rejected(self.cookie_result(good, bad))
        self.assert_login_rejected(self.cookie_result(good, good))

    def test_duplicate_flag_attributes_are_rejected(self):
        for attrs in ("Path=/; Secure; Secure; HttpOnly", "Path=/; Secure; HttpOnly; HttpOnly"):
            with self.subTest(attrs=attrs):
                self.assert_login_rejected(self.cookie_result(
                    "Set-Cookie: __Host-two_session=<COOKIE>; " + attrs))

    def test_whitespace_around_attribute_values_matches_curl(self):
        result = self.cookie_result(
            "Set-Cookie: __Host-two_session=<COOKIE>; Path = / ; Secure ; HttpOnly")
        self.assertEqual(result["rc"], 0, "curl applies WSP-trimmed attributes; the probe must agree")
        self.assertEqual([e["step"] for e in self.http(result)], STEP_IDS)
        self.assert_push(result, True)

    def test_benign_attributes_and_trailing_separator_are_accepted(self):
        result = self.cookie_result(
            "Set-Cookie: __Host-two_session=<COOKIE>; Path=/; Secure; HttpOnly; SameSite=Lax;")
        self.assertEqual(result["rc"], 0)
        self.assert_push(result, True)

    # --- duplicate response evidence ---

    def test_duplicate_origin_values_are_rejected(self):
        for headers in (["x-two-origin: two-web-next", "x-two-origin: wrong"],
                        ["x-two-origin: wrong", "x-two-origin: two-web-next"],
                        ["x-two-origin: two-web-next", "x-two-origin: two-web-next"]):
            with self.subTest(headers=headers):
                result = self.run_probe({"up": {"headers": headers}})
                self.assertEqual(result["rc"], 1, "duplicate origin evidence was accepted")
                self.assertEqual([e["step"] for e in self.http(result)], STEP_IDS)
                self.assert_push(result, False)

    def test_duplicate_json_keys_are_rejected(self):
        bodies = (
            '{"authenticated":false,"authenticated":true}',
            '{"authenticated":true,"authenticated":false}',
            '{"authenticated":true,"authenticated":true}',
            '{"authenticated":false,"\\u0061uthenticated":true}',
        )
        for body in bodies:
            with self.subTest(body=body):
                result = self.run_probe({"auth_status": {"body": body}})
                self.assertEqual(result["rc"], 1, "duplicate-key auth body was accepted")
                self.assertEqual([e["step"] for e in self.http(result)], ["login", "auth_status"])
                self.assert_push(result, False)

    def test_non_finite_json_constants_are_rejected(self):
        for body in ('{"authenticated":true,"pad":NaN}', '{"authenticated":true,"pad":Infinity}'):
            with self.subTest(body=body):
                result = self.run_probe({"auth_status": {"body": body}})
                self.assertEqual(result["rc"], 1, "non-finite JSON constant was accepted")
                self.assertEqual([e["step"] for e in self.http(result)], ["login", "auth_status"])
                self.assert_push(result, False)

    # --- ambient curl-config guard ---

    def test_every_staging_call_disables_ambient_curl_config_first(self):
        result = self.run_probe()
        self.assertEqual(result["rc"], 0)
        calls = self.http(result)
        self.assertEqual([e["step"] for e in calls], STEP_IDS)
        for event in calls:
            with self.subTest(step=event["step"]):
                self.assertEqual(event["argv"][0], "-q",
                                 "ambient .curlrc guard must be the first curl argument")
                self.assertIn("--config", event["argv"])

    def test_retry_calls_keep_the_config_guard_first(self):
        result = self.run_probe({"login_attempts": [
            {"status": 429, "headers": ["Retry-After: 0"]}, {"status": 429}]})
        self.assertEqual(result["rc"], 0)
        logins = self.http(result, "login")
        self.assertEqual(len(logins), 2)
        for event in logins:
            self.assertEqual(event["argv"][0], "-q")

    # --- abort and push-failure outcomes ---

    def test_empty_gatus_url_fails_a_successful_journey(self):
        result = self.run_probe(flags=("--json",), env_overrides={"GATUS_URL": ""})
        self.assertEqual(result["rc"], 1)
        document = self.failed_document(result, "gatus_push_failed")
        self.assertEqual([s["id"] for s in document["steps"]], STEP_IDS)
        self.assertEqual([e for e in result["events"] if e["kind"] == "gatus"], [])

    def test_empty_gatus_url_fails_an_already_failed_journey(self):
        result = self.run_probe({"login": {"cookie": "none"}}, flags=("--json",),
                                env_overrides={"GATUS_URL": ""})
        self.assertEqual(result["rc"], 1)
        self.failed_document(result, "gatus_push_failed")

    def test_sigint_aborts_with_130_and_probe_aborted(self):
        result = self.run_probe(flags=("--json",), block_step="login",
                                deliver_signal=signal.SIGINT)
        self.assertEqual(result["rc"], 130)
        self.failed_document(result, "probe_aborted")

    def test_sigterm_aborts_with_143_and_probe_aborted(self):
        result = self.run_probe(flags=("--json",), block_step="auth_status",
                                deliver_signal=signal.SIGTERM)
        self.assertEqual(result["rc"], 143)
        self.failed_document(result, "probe_aborted")

    def test_sighup_reports_an_uncoded_post_preflight_exit_as_aborted(self):
        result = self.run_probe(flags=("--json",), block_step="login",
                                deliver_signal=signal.SIGHUP)
        self.assertEqual(result["rc"], 129)
        self.failed_document(result, "probe_aborted")

    # --- response size bound ---

    def test_oversize_body_fails_closed_without_leaking_temp_state(self):
        result = self.run_probe({"login": {"body": "x" * OVERSIZE}}, flags=("--json",))
        self.assertEqual(result["rc"], 1)
        self.failed_document(result, "login_transport")
        self.assertEqual([e["step"] for e in self.http(result)], ["login"])
        self.assert_push(result, False)

    def test_oversize_headers_fail_closed(self):
        result = self.run_probe({"login": {"headers": ["X-Pad: " + "y" * OVERSIZE]}},
                                flags=("--json",))
        self.assertEqual(result["rc"], 1)
        self.failed_document(result, "login_transport")
        self.assert_push(result, False)


if __name__ == "__main__":
    unittest.main()
