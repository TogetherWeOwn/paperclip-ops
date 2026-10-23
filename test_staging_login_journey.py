"""Offline acceptance contract for the real staging-login entrypoint.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_staging_login_journey

Test-owned immutable handoff: implementation changes belong outside this file.
No installation, release, cron, threshold or live-probe authority is implied.

The minimal v1 JSON envelope is schema_version=1, probe_id (the existing Gatus
key), outcome (ok/failed/refused/throttled/dry_run), and steps. Step IDs are login,
auth_status and up, in journey order; attempted steps expose http_status and
latency_ms. A failed/refused/throttled result has error.code, error.retryable
and error.next_action. Code spelling is not prescribed: it must be a stable
machine identifier, not interpolated response text. This is a source interface,
not a new retry/deployment/authorization policy.

Fixtures are adversarial HTTP responses, not an authentication implementation.
The curl adapter writes a Netscape jar ONLY for an explicit cookie-jar option.
It records whether auth/status actually received that fresh jar; the freshness
tests require this independently of the returned body. Response-validation tests
intentionally allow a backend to claim authenticated:true without a cookie, so
a missing jar cannot mask the independently broken response validators. No
fixture fabricates cookie propagation on the entrypoint's behalf. All credential
and cookie values are generated per invocation, never literal fixture values.
"""

import ctypes
import errno
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parent
ENTRYPOINT = ROOT / "ops/case_a2ab2937/push-staging-login-smoke.sh"
ORIGIN = "https://next.togetherweown.com"
PROBE_ID = "two_staging-login-smoke"
STEP_IDS = ["login", "auth_status", "up"]

# Only these local, read-only processing tools are exposed. Mutation tools below
# additionally constrain every target to this invocation's disposable directory.
READ_TOOLS = ("dirname", "basename", "grep", "tr", "cat", "cut", "sed", "awk",
              "head", "tail", "wc", "date", "stat", "python3", "jq")

ADAPTER = r'''
import json, os, pathlib, re, shlex, stat, subprocess, sys, urllib.parse

name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ["FIXTURE_ROOT"]).resolve()
scenario = json.loads((root / "scenario.json").read_text())
values = [os.environ[k] for k in ("FIXTURE_QA", "FIXTURE_GATUS", "FIXTURE_COOKIE", "FIXTURE_OLD_COOKIE")]

def record(**event):
    with (root / "events.jsonl").open("a") as out:
        out.write(json.dumps(event, sort_keys=True) + "\n")

def refuse(reason):
    record(kind="violation", reason=reason, command=name)
    sys.stderr.write("offline fixture refused unexpected operation\n")
    raise SystemExit(97)

def local(value):
    p = pathlib.Path(value).resolve()
    if not p.is_relative_to(root):
        refuse("path outside disposable fixture")
    return p

def private(value, directory=False):
    p = local(value)
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode != (0o700 if directory else 0o600):
        refuse("non-private temporary permissions")
    return p

def option(*flags):
    for i, arg in enumerate(args):
        for flag in flags:
            if arg == flag:
                if i + 1 == len(args):
                    refuse("missing option value")
                return args[i + 1]
            if flag.startswith("--") and arg.startswith(flag + "="):
                return arg.split("=", 1)[1]
    return None

if any(value in arg for value in values for arg in args):
    refuse("credential or cookie exposed in argv")

if name == "curl":
    # No real curl binary is reachable through this PATH.
    with (root / "events.jsonl").open() as f:
        previous = [json.loads(line) for line in f]
    if sum(e.get("kind") == "http" for e in previous) >= 6:
        refuse("unbounded HTTP attempts")
    flags = {"-s", "-S", "-sS", "-q", "--disable", "--silent", "--show-error", "--fail", "--fail-with-body"}
    valued = {"--config", "-K", "-D", "--dump-header", "-o", "--output", "-w", "--write-out",
              "--max-time", "--connect-timeout", "--max-redirs", "-X", "--request",
              "-b", "--cookie", "-c", "--cookie-jar", "--url"}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in flags or arg.startswith(("https://", "http://")):
            i += 1
        elif arg in valued:
            if i + 1 == len(args):
                refuse("missing curl value")
            i += 2
        elif "=" in arg and arg.split("=", 1)[0] in valued:
            i += 1
        else:
            refuse("unexpected curl flag or destination")
    config = option("--config", "-K")
    config_lines = []
    if config:
        config_path = private(config)
        private(config_path.parent, directory=True)
        config_lines = config_path.read_text().splitlines()
    entries = {}
    for line in config_lines:
        tokens = shlex.split(line.replace("=", " ", 1))
        if tokens:
            entries.setdefault(tokens[0], []).append(" ".join(tokens[1:]))
    if any(k in entries for k in ("location", "location-trusted", "next", "proxy", "preproxy")):
        refuse("redirect or proxy configured")
    targets = [a for a in args if a.startswith(("http://", "https://"))]
    url = targets[-1] if targets else option("--url")
    url = url or (entries.get("url") or [None])[-1]
    if not url:
        refuse("no explicit destination")
    timeout = option("--max-time") or (entries.get("max-time") or [None])[-1]
    if timeout is None or not 0 < float(timeout) <= 20:
        refuse("unbounded curl timeout")
    output = option("-o", "--output")
    headers = option("-D", "--dump-header")
    if url.startswith("https://gatus.invalid/"):
        parsed = urllib.parse.urlsplit(url)
        if parsed.netloc != "gatus.invalid" or parsed.path != "/api/v1/endpoints/two_staging-login-smoke/external":
            refuse("unexpected Gatus destination")
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("success") not in (["true"], ["false"]):
            refuse("unexpected Gatus verdict")
        if "Authorization: Bearer " + os.environ["FIXTURE_GATUS"] not in entries.get("header", []):
            refuse("missing private Gatus authentication")
        if (option("-X", "--request") or (entries.get("request") or [""])[-1]) != "POST":
            refuse("unexpected Gatus method")
        if any(v in url for v in values) or "DO_NOT_DUMP" in urllib.parse.unquote(url):
            refuse("response or credential dumped to Gatus")
        record(kind="gatus", success=query["success"][0] == "true", private_config=True)
        sys.stdout.write("204")
        raise SystemExit(0)
    paths = {"/auth/qa/qa-member": "login", "/auth/status": "auth_status", "/up": "up"}
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "next.togetherweown.com" or parsed.query or parsed.fragment or parsed.path not in paths:
        refuse("unexpected staging destination")
    step = paths[parsed.path]
    method = option("-X", "--request") or "GET"
    if method != ("POST" if step == "login" else "GET"):
        refuse("unexpected staging method")
    # Curl defaults to no following; -L is already rejected by the flag allowlist.
    if option("--max-redirs") not in (None, "0"):
        refuse("redirect limit changed")
    if not output or not headers:
        refuse("missing bounded response files")
    body_path, header_path = local(output), local(headers)
    private(body_path.parent, directory=True)
    private(header_path.parent, directory=True)
    jar_out = option("-c", "--cookie-jar") or (entries.get("cookie-jar") or [None])[-1]
    jar_in = option("-b", "--cookie") or (entries.get("cookie") or [None])[-1]
    incoming = local(jar_in).read_text() if jar_in and "=" not in jar_in else ""
    fresh = "\t__Host-two_session\t" + os.environ["FIXTURE_COOKIE"] in incoming
    stale = os.environ["FIXTURE_OLD_COOKIE"] in incoming
    prior_login = [e for e in previous if e.get("kind") == "http" and e.get("step") == "login"]
    attempt = len(prior_login)
    jar_empty = True
    if jar_out:
        jar = local(jar_out)
        if jar.exists():
            private(jar)
            jar_empty = not jar.read_text().strip()
    response = scenario.get(step, {})
    if step == "login":
        response = scenario.get("login_attempts", [scenario.get("login", {})])[min(attempt, len(scenario.get("login_attempts", [{}])) - 1)]
        if "X-TWO-QA-Auth: " + os.environ["FIXTURE_QA"] not in entries.get("header", []):
            refuse("missing private QA authentication")
        if "Origin: https://next.togetherweown.com" not in entries.get("header", []):
            refuse("missing staging Origin")
    if jar_in:
        private(jar_in)
    code = response.get("status", 204 if step == "login" else 200)
    seconds = response.get("seconds", {"login": 0.012, "auth_status": 0.023, "up": 0.034}[step])
    if step == "login":
        cookie = response.get("cookie", "valid")
        value = os.environ["FIXTURE_COOKIE"]
        cookie_lines = {
            "valid": ["Set-Cookie: __Host-two_session=" + value + "; Path=/; Secure; HttpOnly"],
            "mixed_case": ["sEt-CoOkIe: __Host-two_session=" + value + "; Path=/; sEcUrE; hTtPoNlY"],
            "none": [],
            "unrelated_headers": ["Set-Cookie: __Host-two_session=" + value + "; Path=/", "X-Flags: Secure HttpOnly"],
            "different_cookie": ["Set-Cookie: __Host-two_session=" + value + "; Path=/", "Set-Cookie: other=public-marker; Secure; HttpOnly"],
            "split_attributes": ["Set-Cookie: __Host-two_session=" + value + "; Path=/; Secure", "Set-Cookie: other=public-marker; HttpOnly"],
            "attribute_substrings": ["Set-Cookie: __Host-two_session=" + value + "; Path=/; NotSecure; NotHttpOnly"],
            "name_prefix": ["Set-Cookie: not__Host-two_session=" + value + "; Secure; HttpOnly"],
            "not_set_cookie": ["X-Session: __Host-two_session=" + value + "; Secure; HttpOnly"],
        }[cookie]
        extra_headers = cookie_lines + response.get("headers", [])
        # Emulate curl, not the desired validators: even an insecure Set-Cookie
        # record can enter a jar. Other response headers can never create one.
        if jar_out:
            lines = ["# Netscape HTTP Cookie File"]
            for line in cookie_lines:
                if line.lower().startswith("set-cookie: __host-two_session="):
                    lines.append("#HttpOnly_next.togetherweown.com\tFALSE\t/\tTRUE\t0\t__Host-two_session\t" + value)
            if response.get("poison_jar"):
                lines.append("#HttpOnly_next.togetherweown.com\tFALSE\t/\tTRUE\t0\t__Host-two_session\t" + os.environ["FIXTURE_OLD_COOKIE"])
            jar.write_text("\n".join(lines) + "\n")
            jar.chmod(0o600)
    else:
        extra_headers = response.get("headers", ["x-two-origin: two-web-next"] if step == "up" else [])
    if scenario.get("require_fresh_cookie") and step == "auth_status" and not fresh:
        code = 401
    record(kind="http", step=step, attempt=attempt + 1 if step == "login" else 1,
           status=code, seconds=seconds, cookie_jar_written=bool(jar_out),
           fresh_cookie_received=fresh, stale_cookie_received=stale,
           jar_empty_before_login=jar_empty, cookie_read=bool(jar_in),
           private_config=bool(config), private_jar=bool(jar_in or jar_out),
           header_empty_before=not header_path.exists() or header_path.stat().st_size == 0,
           body_empty_before=not body_path.exists() or body_path.stat().st_size == 0)
    body = response.get("body", '{"authenticated":true}' if step == "auth_status" else "")
    header_path.write_text("HTTP/1.1 " + str(code) + " Fixture\r\n" + "\r\n".join(extra_headers) + "\r\n\r\n")
    body_path.write_text(body)
    if response.get("transport_error"):
        sys.stderr.write("DO_NOT_DUMP_TRANSPORT " + os.environ["FIXTURE_QA"] + "\n")
        raise SystemExit(28)
    sys.stdout.write(str(code) + " " + str(seconds))
elif name == "sleep":
    if len(args) != 1 or not re.fullmatch(r"[0-9]+", args[0]) or not 0 <= int(args[0]) <= 120:
        refuse("unbounded sleep")
    record(kind="sleep", seconds=int(args[0]))
elif name in ("mktemp", "chmod", "rm"):
    if name == "mktemp":
        paths = [a for a in args if not a.startswith("-")]
        if len(paths) != 1:
            refuse("unexpected mktemp shape")
        local(paths[0])
    elif name == "chmod":
        if len(args) < 2 or args[0] not in ("600", "0600", "700", "0700"):
            refuse("unexpected chmod shape")
        for p in args[1:]:
            local(p)
    else:
        for p in args:
            if not p.startswith("-"):
                local(p)
    real = json.loads(os.environ["FIXTURE_TOOLS"])[name]
    result = subprocess.run([real, *args], capture_output=True, timeout=3)
    if name == "mktemp" and result.returncode == 0:
        p = local(result.stdout.decode().strip())
        private(p, directory=p.is_dir())
        # A pre-existing session must not make a new attempt pass.
        if p.is_dir() and p.name.startswith("login_smoke."):
            (p / "jar").write_text("#HttpOnly_next.togetherweown.com\tFALSE\t/\tTRUE\t0\t__Host-two_session\t" + os.environ["FIXTURE_OLD_COOKIE"] + "\n")
            (p / "jar").chmod(0o600)
    record(kind=name)
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    raise SystemExit(result.returncode)
else:
    tools = json.loads(os.environ["FIXTURE_TOOLS"])
    if name not in tools:
        refuse("unknown command")
    record(kind="local", command=name)
    result = subprocess.run([tools[name], *args], timeout=3)
    raise SystemExit(result.returncode)
'''


class StagingLoginJourneyTests(unittest.TestCase):
    """Every test invokes unmodified tracked source, never a copied probe."""

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

        # Ubuntu CI provides libseccomp. Refuse to run without a real network
        # fence: PATH stubs alone cannot contain an absolute-path curl command.
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

    def run_probe(self, scenario=None, flags=(), env_overrides=None):
        parent = os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR") or os.environ.get("PAPERCLIP_SCRATCH_DIR") or os.environ.get("TMPDIR")
        with tempfile.TemporaryDirectory(prefix="staging-journey-test-", dir=parent) as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "tmp").mkdir(mode=0o700)
            (root / "home").mkdir(mode=0o700)
            (root / "events.jsonl").touch()
            (root / "scenario.json").write_text(json.dumps(scenario or {}))
            adapter = root / "adapter"
            adapter.write_text("#!" + sys.executable + "\n" + textwrap.dedent(ADAPTER))
            adapter.chmod(0o700)
            for name in (*self.tools, "curl", "sleep"):
                (root / "bin" / name).symlink_to(adapter)
            # No /usr/bin or /bin fallback: an unrecognized external command
            # terminates the journey instead of accidentally reaching the host.
            guard = root / "guard.sh"
            guard.write_text(textwrap.dedent('''\
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
            '''))
            qa, gatus, cookie, old_cookie = (secrets.token_hex(24) for _ in range(4))
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

    def http(self, result, step=None):
        return [e for e in result["events"] if e["kind"] == "http" and (step is None or e["step"] == step)]

    def assert_push(self, result, success):
        self.assertEqual([e["success"] for e in result["events"] if e["kind"] == "gatus"], [success])

    def assert_no_io(self, result):
        self.assertEqual([e for e in result["events"] if e["kind"] != "local"], [],
                         "static/refusal mode attempted session/config/push/sleep activity")

    def machine(self, result, outcome):
        try:
            document = json.loads(result["stdout"])
        except (ValueError, TypeError):
            self.fail("--json must emit exactly one JSON result, not legacy text or response dumps")
        self.assertIsInstance(document, dict)
        self.assertIs(type(document.get("schema_version")), int)
        self.assertEqual(document.get("schema_version"), 1)
        self.assertEqual(document.get("probe_id"), PROBE_ID)
        self.assertEqual(document.get("outcome"), outcome)
        self.assertIsInstance(document.get("steps"), list)
        ids = [step.get("id") for step in document["steps"]]
        self.assertEqual(len(ids), len(set(ids)), "duplicate step IDs")
        self.assertTrue(all(i in STEP_IDS for i in ids))
        self.assertEqual(ids, [i for i in STEP_IDS if i in ids])
        for step in document["steps"]:
            observed = self.http(result, step["id"])
            if not observed:
                continue
            last = observed[-1]
            self.assertEqual(step.get("http_status"), last["status"])
            latency = step.get("latency_ms")
            self.assertIn(type(latency), (int, float))
            self.assertTrue(math.isfinite(latency))
            self.assertAlmostEqual(latency, last["seconds"] * 1000, places=3)
        serialized = json.dumps(document)
        self.assertNotIn("DO_NOT_DUMP", serialized)
        for forbidden in ("Set-Cookie:", "X-TWO-QA-Auth:", "Authorization: Bearer", "#HttpOnly_"):
            self.assertNotIn(forbidden, serialized)
        if outcome in ("failed", "refused", "throttled"):
            error = document.get("error")
            self.assertIsInstance(error, dict)
            self.assertRegex(error.get("code", ""), r"^[A-Za-z][A-Za-z0-9_.-]{2,63}$")
            self.assertIs(type(error.get("retryable")), bool)
            self.assertIsInstance(error.get("next_action"), str)
            self.assertTrue(error["next_action"].strip())
        return document

    def test_fresh_login_cookie_is_written_and_consumed_by_auth_status(self):
        result = self.run_probe({"require_fresh_cookie": True})
        login = self.http(result, "login")[0]
        status = self.http(result, "auth_status")[0]
        self.assertTrue(login["cookie_jar_written"], "successful POST never enabled curl cookie-jar writing")
        self.assertTrue(login["jar_empty_before_login"], "an inherited session survived login initialization")
        self.assertTrue(status["fresh_cookie_received"], "auth/status did not receive this login's cookie")
        self.assertFalse(status["stale_cookie_received"])
        self.assertEqual(result["rc"], 0)
        self.assert_push(result, True)

    def test_positive_text_control_retains_three_steps_and_unauthenticated_up(self):
        result = self.run_probe()
        self.assertEqual(result["rc"], 0)
        self.assertEqual([e["step"] for e in self.http(result)], STEP_IDS)
        self.assertFalse(self.http(result, "up")[0]["cookie_read"])
        self.assertTrue(result["stdout"].strip())
        self.assert_push(result, True)

    def test_cookie_attributes_belong_to_exact_set_cookie_record(self):
        for cookie in ("unrelated_headers", "different_cookie", "split_attributes",
                       "attribute_substrings", "name_prefix", "not_set_cookie", "none"):
            with self.subTest(cookie=cookie):
                result = self.run_probe({"login": {"cookie": cookie}})
                self.assertEqual(result["rc"], 1, "invalid session cookie was accepted: " + cookie)
                self.assertEqual([e["step"] for e in self.http(result)], ["login"])
                self.assert_push(result, False)

    def test_http_header_and_attribute_case_positive_control(self):
        result = self.run_probe({"login": {"cookie": "mixed_case"},
                                 "up": {"headers": ["X-TwO-OrIgIn:\t two-web-next \t"]}})
        self.assertEqual(result["rc"], 0, "normal HTTP case/OWS must not be rejected")
        self.assert_push(result, True)

    def test_authentication_is_a_top_level_json_boolean_not_a_regex_match(self):
        bodies = (
            '{"authenticated":"true"}', '{"authenticated":1}', '{"authenticated":false}',
            '{"nested":{"authenticated":true}}', '[{"authenticated":true}]',
            '{"authenticated":true} DO_NOT_DUMP_TRAILER', '{"authenticated":true',
            '{"authenticated":trueish}', '"authenticated":true', 'null',
            '{"authenticated":null}', '{}',
        )
        for body in bodies:
            with self.subTest(body=body):
                result = self.run_probe({"auth_status": {"body": body}})
                self.assertEqual(result["rc"], 1, "wrong-type/malformed/nested-only auth body accepted")
                self.assertEqual([e["step"] for e in self.http(result)], ["login", "auth_status"])
                self.assert_push(result, False)

    def test_json_whitespace_and_additional_fields_positive_control(self):
        result = self.run_probe({"auth_status": {"body": ' {\n "authenticated" : true, "other": [1,2] }\n'}})
        self.assertEqual(result["rc"], 0)
        self.assert_push(result, True)

    def test_origin_value_is_exact_not_a_prefix_or_unrelated_header(self):
        for headers in (["x-two-origin: two-web-next-spoof"], ["x-two-origin: two-web-next.example"],
                        ["x-two-origin: two-web-next, attacker"], ["X-Other: x-two-origin: two-web-next"],
                        ["x-two-origin: wrong"], []):
            with self.subTest(headers=headers):
                result = self.run_probe({"up": {"headers": headers}})
                self.assertEqual(result["rc"], 1, "non-exact origin response was accepted")
                self.assertEqual([e["step"] for e in self.http(result)], STEP_IDS)
                self.assert_push(result, False)

    def test_retry_clears_cookie_and_response_state_then_uses_fresh_session(self):
        result = self.run_probe({"require_fresh_cookie": True, "login_attempts": [
            {"status": 429, "poison_jar": True, "headers": ["Retry-After: 2"], "body": "DO_NOT_DUMP_RETRY"},
            {"status": 204},
        ]})
        logins = self.http(result, "login")
        self.assertEqual(len(logins), 2)
        self.assertTrue(all(e["cookie_jar_written"] for e in logins), "both login attempts must capture cookies")
        self.assertTrue(all(e["jar_empty_before_login"] for e in logins), "retry inherited a throttled session")
        self.assertTrue(all(e["header_empty_before"] and e["body_empty_before"] for e in logins))
        self.assertTrue(self.http(result, "auth_status")[0]["fresh_cookie_received"])
        self.assertFalse(self.http(result, "auth_status")[0]["stale_cookie_received"])
        self.assertEqual([e["seconds"] for e in result["events"] if e["kind"] == "sleep"], [2])
        self.assertEqual(result["rc"], 0)
        self.assert_push(result, True)

    def test_retry_cannot_reuse_cookie_headers_from_a_throttled_attempt(self):
        result = self.run_probe({"login_attempts": [
            {"status": 429, "headers": ["Retry-After: 0"]},
            {"status": 204, "cookie": "none"},
        ]})
        self.assertEqual(result["rc"], 1)
        self.assertEqual(len(self.http(result, "login")), 2)
        self.assertEqual(self.http(result, "auth_status"), [])
        self.assert_push(result, False)

    def test_bounded_429_policy_preserves_text_exit_and_push(self):
        for retry_after, expected in (("999", 120), ("not-a-number", 60), (None, 60)):
            with self.subTest(retry_after=retry_after):
                headers = [] if retry_after is None else ["Retry-After: " + retry_after]
                result = self.run_probe({"login_attempts": [{"status": 429, "headers": headers}, {"status": 429}]})
                self.assertEqual(result["rc"], 0)
                self.assertEqual([e["step"] for e in self.http(result)], ["login", "login"])
                self.assertEqual([e["seconds"] for e in result["events"] if e["kind"] == "sleep"], [expected])
                self.assert_push(result, True)

    def test_status_redirect_transport_and_latency_fail_closed_without_dumping(self):
        for step, response in (("login", {"status": 302, "headers": ["Location: https://redirect.invalid"]}),
                               ("auth_status", {"status": 401}), ("up", {"status": 503}),
                               ("login", {"seconds": 5.0}), ("auth_status", {"seconds": 3.0}),
                               ("up", {"seconds": 3.0}), ("login", {"transport_error": True})):
            with self.subTest(step=step, response=response):
                result = self.run_probe({step: response})
                self.assertEqual(result["rc"], 1)
                self.assertEqual([e["step"] for e in self.http(result)], STEP_IDS[:STEP_IDS.index(step) + 1])
                self.assertNotIn("DO_NOT_DUMP", result["stdout"] + result["stderr"])
                self.assert_push(result, False)

    def test_json_success_is_versioned_and_contains_observed_step_metrics(self):
        result = self.run_probe(flags=("--json",))
        self.assertEqual(result["rc"], 0)
        document = self.machine(result, "ok")
        self.assertEqual([step["id"] for step in document["steps"]], STEP_IDS)
        self.assert_push(result, True)

    def test_json_errors_are_stable_redacted_and_actionable(self):
        for scenario in ({"login": {"cookie": "none"}},
                         {"auth_status": {"body": '{"nested":{"authenticated":true},"note":"DO_NOT_DUMP_BODY"}'}},
                         {"up": {"headers": ["x-two-origin: two-web-next-spoof", "X-Diagnostic: DO_NOT_DUMP_HEADER"]}}):
            with self.subTest(scenario=scenario):
                first = self.run_probe(scenario, flags=("--json",))
                self.assertEqual(first["rc"], 1)
                document = self.machine(first, "failed")
                self.assertEqual([s["id"] for s in document["steps"]][:len(self.http(first))],
                                 [e["step"] for e in self.http(first)])
                self.assertNotIn("DO_NOT_DUMP", first["stdout"] + first["stderr"])
                second = self.run_probe(scenario, flags=("--json",))
                self.assertEqual(second["rc"], 1)
                again = self.machine(second, "failed")
                self.assertEqual(document["error"], again["error"], "error identity/remediation changed between equivalent runs")
                self.assert_push(first, False)

    def test_json_throttled_is_not_authenticated_journey_success(self):
        result = self.run_probe({"login_attempts": [{"status": 429, "headers": ["Retry-After: 0"]}, {"status": 429}]},
                                flags=("--json",))
        self.assertEqual(result["rc"], 0, "structured mode must preserve the non-paging throttle exit")
        document = self.machine(result, "throttled")
        self.assertIn("login", [step["id"] for step in document["steps"]])
        self.assertTrue(document["error"]["retryable"])
        self.assertNotEqual(document.get("authenticated"), True)
        self.assert_push(result, True)

    def test_dry_run_is_static_even_without_credentials(self):
        for flags in (("--dry-run",), ("--json", "--dry-run"), ("--dry-run", "--json")):
            with self.subTest(flags=flags):
                result = self.run_probe(flags=flags, env_overrides={"QA_AUTH_TOKEN": "", "GATUS_EXTERNAL_TOKEN": ""})
                self.assertEqual(result["rc"], 0, "static plan must not demand credentials or attempt a session")
                self.assert_no_io(result)
                if "--json" in flags:
                    document = self.machine(result, "dry_run")
                    self.assertEqual([s["id"] for s in document["steps"]], STEP_IDS)
                else:
                    self.assertTrue(result["stdout"].strip())

    def test_dry_run_with_valid_environment_still_never_creates_secret_config(self):
        result = self.run_probe(flags=("--dry-run",))
        self.assertEqual(result["rc"], 0)
        self.assert_no_io(result)

    def test_unknown_conflicting_and_positional_flags_refuse_before_io(self):
        for flags in (("--unknown",), ("execute",), ("--dry-run=false",),
                      ("--dry-run", "--execute"), ("--json", "--json=false")):
            with self.subTest(flags=flags):
                result = self.run_probe(flags=flags)
                self.assertEqual(result["rc"], 2, "unrecognized/conflicting flags must not run the journey")
                self.assert_no_io(result)

    def test_json_refusal_is_actionable_without_requesting_or_echoing_credentials(self):
        result = self.run_probe(flags=("--json",), env_overrides={"QA_AUTH_TOKEN": ""})
        self.assertEqual(result["rc"], 2)
        self.assert_no_io(result)
        self.machine(result, "refused")

    def test_help_is_offline_and_discovers_the_machine_plan_and_check(self):
        result = self.run_probe(flags=("--help",), env_overrides={"QA_AUTH_TOKEN": "", "GATUS_EXTERNAL_TOKEN": ""})
        self.assertEqual(result["rc"], 0)
        self.assert_no_io(result)
        for flag in ("--json", "--dry-run"):
            self.assertIn(flag, result["stdout"])
        self.assertIn("test_staging_login_journey", result["stdout"])

    def test_exact_staging_origin_refusal_remains_before_any_io(self):
        for base in (ORIGIN + ".invalid", ORIGIN + ":443", ORIGIN + "/path", ORIGIN + "//",
                     "https://attacker.invalid/next.togetherweown.com", "http://next.togetherweown.com"):
            with self.subTest(base=base):
                result = self.run_probe(env_overrides={"STAGING_BASE_URL": base})
                self.assertEqual(result["rc"], 2)
                self.assert_no_io(result)

    def test_canonical_origin_with_one_trailing_slash_positive_control(self):
        result = self.run_probe(env_overrides={"STAGING_BASE_URL": ORIGIN + "/"})
        self.assertEqual(result["rc"], 0)
        self.assertEqual([e["step"] for e in self.http(result)], STEP_IDS)
        self.assert_push(result, True)

    def test_missing_and_unsafe_credentials_refuse_before_config_creation(self):
        for value in ("", "not safe\nheader: injection"):
            with self.subTest(value=value):
                result = self.run_probe(env_overrides={"QA_AUTH_TOKEN": value})
                self.assertEqual(result["rc"], 2)
                self.assert_no_io(result)


if __name__ == "__main__":
    unittest.main()
