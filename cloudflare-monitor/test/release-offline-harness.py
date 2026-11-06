"""Execute an opaque workflow Python scalar with only synthetic provider I/O."""

import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import sysconfig
import urllib.error
import urllib.parse
import urllib.request


request = json.load(sys.stdin)
workspace = Path(request["workspace"]).resolve()
os.chdir(workspace)
os.environ.clear()
os.environ.update(request["env"])
os.environ.update({
    "HOME": str(workspace),
    "RUNNER_TEMP": str(workspace),
    "GITHUB_WORKSPACE": str(workspace),
    "GITHUB_OUTPUT": str(workspace / "github-output"),
    "PATH": str(workspace / "offline-bin"),
})
sys.argv = ["-"]
calls = []
commands = []
guard_failures = []
mutations = []
unexpected = []
stdout = io.StringIO()
stderr = io.StringIO()


class FixtureResponse(io.BytesIO):
    def __init__(self, body, url, status, headers):
        super().__init__(body)
        self.url = url
        self.status = status
        self.headers = headers

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def info(self):
        return self.headers


def mock_urlopen(value, data=None, timeout=None, **kwargs):
    del timeout, kwargs
    if isinstance(value, urllib.request.Request):
        url = value.full_url
        method = value.get_method()
    else:
        url = str(value)
        method = "POST" if data is not None else "GET"
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    token_key = "GH_TOKEN" if parsed.hostname == "api.github.com" else "PLAN_READ_TOKEN"
    token = os.environ.get(token_key, "")
    authorization = value.get_header("Authorization", "") if isinstance(value, urllib.request.Request) else ""
    accepted = ("Bearer " + token, "token " + token) if parsed.hostname == "api.github.com" else ("Bearer " + token,)
    authenticated = bool(token) and authorization in accepted
    call = {"method": method, "host": parsed.hostname, "path": parsed.path, "query": query, "authenticated": authenticated}
    calls.append(call)
    if method not in ("GET", "HEAD"):
        mutations.append(call)
        raise RuntimeError("offline_provider_mutation_denied")
    if len(calls) > 24:
        guard_failures.append("provider_request_bound")
        raise RuntimeError("offline_provider_request_bound")
    for route in request.get("routes", []):
        if route.get("method", "GET") != method or route["host"] != parsed.hostname or route["path"] != parsed.path:
            continue
        if "query" in route and any(query.get(key) != [str(expected)] for key, expected in route["query"].items()):
            continue
        if "query" not in route and query.get("page", ["1"]) != ["1"]:
            continue
        call["fixture"] = route.get("id", "provider-fixture")
        if not authenticated:
            raise urllib.error.HTTPError(url, 401, "example-only authorization denied", http.client.HTTPMessage(), io.BytesIO(b"{}"))
        if route.get("transportError"):
            raise urllib.error.URLError("example-only transport failure")
        status = route.get("status", 200)
        body = route.get("raw", json.dumps(route.get("json"))).encode()
        headers = http.client.HTTPMessage()
        for key, value in route.get("headers", {}).items():
            headers[key] = value
        if not 200 <= status < 300:
            raise urllib.error.HTTPError(url, status, "example-only provider failure", headers, io.BytesIO(body))
        return FixtureResponse(body, url, status, headers)
    unexpected.append(call)
    raise RuntimeError("unexpected_offline_provider_route")


def deny_command(*args, **kwargs):
    del kwargs
    command = args[0] if args else []
    first = command[0] if isinstance(command, (list, tuple)) and command else str(command).split(" ")[0]
    commands.append(Path(str(first)).name)
    raise RuntimeError("offline_provider_command_denied")


def deny_socket(*args, **kwargs):
    del args, kwargs
    guard_failures.append("unmocked_network_access")
    raise RuntimeError("offline_network_denied")


urllib.request.urlopen = mock_urlopen
urllib.request.OpenerDirector.open = lambda self, *args, **kwargs: mock_urlopen(*args, **kwargs)
socket.create_connection = deny_socket
socket.getaddrinfo = deny_socket
socket.socket.connect = deny_socket
socket.socket.connect_ex = deny_socket
for name in ("Popen", "run", "call", "check_call", "check_output"):
    setattr(subprocess, name, deny_command)
os.system = deny_command
shutil.which = lambda name, *args, **kwargs: None if name in request.get("missingCapabilities", []) else "/offline/" + Path(name).name

stdlib = Path(sysconfig.get_path("stdlib")).resolve()


def audit(event, args):
    if event.startswith("socket.") or event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"):
        guard_failures.append("unmocked_external_operation")
        raise RuntimeError("offline_external_operation_denied")
    if event == "open" and args and not isinstance(args[0], int):
        path = Path(os.fsdecode(args[0])).resolve()
        mode = args[1] or ""
        flags = args[2] or 0
        writing = any(letter in str(mode) for letter in "wax+") or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        if path.is_relative_to(workspace):
            return
        if not writing and path.is_relative_to(stdlib):
            return
        guard_failures.append("external_file_access")
        raise RuntimeError("offline_external_file_access_denied")


sys.addaudithook(audit)
exit_code = 0
with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
    try:
        exec(compile(request["program"], "opaque-workflow-step", "exec"), {"__name__": "__main__"})
    except SystemExit as error:
        if error.code is None:
            exit_code = 0
        elif isinstance(error.code, int):
            exit_code = error.code
        else:
            exit_code = 1
            print(str(error.code), file=sys.stderr)
    except Exception as error:
        exit_code = 1
        print(type(error).__name__ + ": " + str(error), file=sys.stderr)

files = {}
for path in sorted(workspace.rglob("*")):
    if path.is_file():
        files[str(path.relative_to(workspace))] = {"text": path.read_text(), "mode": path.stat().st_mode & 0o777}
print(json.dumps({
    "exitCode": exit_code,
    "stdout": stdout.getvalue(),
    "stderr": stderr.getvalue(),
    "calls": calls,
    "commands": commands,
    "guardFailures": guard_failures,
    "mutations": mutations,
    "unexpected": unexpected,
    "files": files,
}))
