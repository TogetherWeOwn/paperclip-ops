#!/usr/bin/env python3
"""Disabled-by-default fixed-purpose carrier around the staging-preflight adapter.

This gate validates one dispatch request against a root-owned installed
manifest and, only when every check passes, execs the fixed installed adapter
as `[interpreter, "-I", <adapter>]` with fixed argv, a scrubbed environment
and no stdin. It performs no network I/O itself and it never searches for an
alternate script, action or entrypoint: any failure refuses, loudly, with a
fixed-vocabulary code. A refused request performs zero I/O.

There is no install, enable or activate mode. Without an installed manifest
that explicitly enables execution, every dispatch refuses. Shipping this file
changes nothing about what may run; installation and activation are separate
decisions owned elsewhere. The offline test suite is the no-network
conformance entry: it traverses this same dispatch function against an
explicitly test-only fixture root and never establishes installed execution.
"""

import argparse
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time

SCHEMA_REQUEST = "staging-preflight-dispatch/v1"
SCHEMA_MANIFEST = "staging-preflight-manifest/v1"
SCHEMA_RECEIPT = "staging-preflight-dispatch-receipt/v1"
KIND_RECEIPT = "staging-preflight-dispatch-receipt"
ACTION = "staging-preflight"

# Fixed installed paths. The command line offers no override: tests inject a
# fixture root through the dispatch function only, never through these.
ADAPTER_PATH = "/usr/local/libexec/paperclip-host-ops/staging-preflight-adapter.py"
MANIFEST_PATH = "/etc/paperclip-host-ops/staging-preflight-manifest.json"
STATE_DIR = "/var/lib/paperclip-host-ops/staging-preflight"

MAX_REQUEST_BYTES = 16 * 1024
MAX_MANIFEST_BYTES = 16 * 1024
MAX_ADAPTER_BYTES = 256 * 1024
MAX_STDOUT_BYTES = 64 * 1024
MAX_STDERR_BYTES = 16 * 1024
MAX_CLAIM_BYTES = 4 * 1024
MAX_CLAIMS = 128
MAX_EPOCH = 2 ** 34
MAX_TARGET_VERSION = 2 ** 31 - 1
PRUNE_AFTER_SECONDS = 86400
DISPATCH_TIMEOUT_SECONDS = 180

CHILD_PATH = "/usr/bin:/bin"
# Exact environment the fixed adapter inherits: existing bindings only, copied
# when present, never invented. Anything else (proxy variables included) is
# refused outright before dispatch or dropped from the child.
CHILD_ENV_KEYS = ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN",
                  "PAPERCLIP_RUN_SCRATCH_DIR", "HOME")
# Fixed child values. Pinning the locale keeps the interpreter from injecting
# its own LC_CTYPE under coercion, so the child environment stays exactly the
# allowlist below plus these three.
FIXED_CHILD_ENV = {"PATH": CHILD_PATH, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
PROXY_VARIABLES = ("https_proxy", "http_proxy", "all_proxy",
                   "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")

REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
APPROVAL_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
HEX40_RE = re.compile(r"[0-9a-f]{40}")
HEX64_RE = re.compile(r"[0-9a-f]{64}")
HEX32_RE = re.compile(r"[0-9a-f]{32}")
UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
BUILD_ID_RE = re.compile(r"[0-9]{1,20}-[0-9]{1,20}")
IMAGE_RE = re.compile(r"registry\.cloudflare\.com/[0-9a-f]{32}/"
                      r"two-bot-next-twobotcontainer-staging@sha256:[0-9a-f]{64}")
FAILURE_RE = re.compile(r"[a-z0-9_]{1,64}")
RAY_RE = re.compile(r"[0-9a-f]{16}-[A-Z]{3}")
CLAIM_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\.claim")

REQUEST_KEYS = frozenset(("schema", "action", "request_id", "principal",
                          "approval_id", "expires_at", "targets"))
MANIFEST_KEYS = frozenset(("schema", "enabled", "adapter_sha256", "approval",
                           "principal", "targets"))
TARGET_KEYS = ("revision", "build_id", "worker_version", "application_id",
               "namespace_id", "rollout_id", "target_version", "image",
               "origin_sha256")
# Adapter receipt keys the carrier understands. Informational keys are
# validated for shape only where cheap and always dropped; anything else
# refuses the receipt so raw payloads can never pass through.
RECEIPT_KEYS = frozenset(("started_utc", "finished_utc", "source_sha",
                          "source_blobs", "result", "failure",
                          "runtime_observations", "runtime", "control_plane",
                          "identity_reread", "staging_origin_fence",
                          "lag_tolerance_used"))
OBSERVATION_KEYS = frozenset(("surface", "status", "worker_version", "ray"))
OBSERVATION_ORDER = ("health", "readyz")


class Refusal(Exception):
    """A dispatch refused before any adapter I/O. Carries a fixed code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Install:
    adapter_path: str
    manifest_path: str
    state_dir: str


def production_install():
    return Install(adapter_path=ADAPTER_PATH, manifest_path=MANIFEST_PATH,
                   state_dir=STATE_DIR)


def _shape(value, expression):
    return isinstance(value, str) and expression.fullmatch(value) is not None


def _epoch(value):
    # Any integer epoch passes shape; value checks below refuse stale ones.
    return type(value) is int and value <= MAX_EPOCH


def read_regular(path, cap, unreadable, too_large):
    """Read a bounded regular file without following symlinks."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Refusal(unreadable) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refusal(unreadable)
        if info.st_size > cap:
            raise Refusal(too_large)
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(cap + 1)
    except OSError as error:
        if isinstance(error, Refusal):
            raise
        raise Refusal(unreadable) from None
    finally:
        os.close(fd)
    if len(data) > cap:
        raise Refusal(too_large)
    return data, info


def parse_request(data):
    try:
        request = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Refusal("request_not_json") from None
    if not isinstance(request, dict) or set(request) != REQUEST_KEYS:
        raise Refusal("request_schema_refused")
    if request["schema"] != SCHEMA_REQUEST or request["action"] != ACTION:
        raise Refusal("request_schema_refused")
    if not _shape(request["request_id"], REQUEST_ID_RE):
        raise Refusal("request_schema_refused")
    if not _shape(request["principal"], USER_RE):
        raise Refusal("request_schema_refused")
    if not _epoch(request["expires_at"]):
        raise Refusal("request_schema_refused")
    validate_targets(request["targets"])
    # approval_id is checked against the installed approval without
    # distinguishing unknown, malformed or absent: one code, no oracle.
    return request


def validate_targets(targets):
    if not isinstance(targets, dict) or set(targets) != set(TARGET_KEYS):
        raise Refusal("request_schema_refused")
    checks = (("revision", HEX40_RE), ("build_id", BUILD_ID_RE),
              ("worker_version", UUID_RE), ("application_id", UUID_RE),
              ("namespace_id", HEX32_RE), ("rollout_id", UUID_RE),
              ("image", IMAGE_RE), ("origin_sha256", HEX64_RE))
    for key, expression in checks:
        if not _shape(targets[key], expression):
            raise Refusal("request_schema_refused")
    version = targets["target_version"]
    if type(version) is not int or not 1 <= version <= MAX_TARGET_VERSION:
        raise Refusal("request_schema_refused")


def parse_manifest(data):
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Refusal("manifest_refused") from None
    if not isinstance(manifest, dict):
        raise Refusal("manifest_refused")
    # Disabled is the default: anything but an explicit true refuses here,
    # before any other manifest complaint, missing key included.
    if manifest.get("enabled") is not True:
        raise Refusal("carrier_disabled")
    if set(manifest) != MANIFEST_KEYS:
        raise Refusal("manifest_refused")
    if manifest["schema"] != SCHEMA_MANIFEST:
        raise Refusal("manifest_refused")
    if not _shape(manifest["adapter_sha256"], HEX64_RE):
        raise Refusal("manifest_refused")
    approval = manifest["approval"]
    if (not isinstance(approval, dict) or set(approval) != {"id", "expires_at"}
            or not _shape(approval["id"], APPROVAL_RE)
            or not _epoch(approval["expires_at"])):
        raise Refusal("manifest_refused")
    principal = manifest["principal"]
    if (not isinstance(principal, dict) or set(principal) != {"user"}
            or not _shape(principal["user"], USER_RE)):
        raise Refusal("manifest_refused")
    try:
        validate_targets(manifest["targets"])
    except Refusal:
        raise Refusal("manifest_refused") from None
    return manifest


def invoker():
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return None


def open_state_dir(path):
    try:
        info = os.lstat(path)
    except OSError:
        raise Refusal("state_refused") from None
    if not stat.S_ISDIR(info.st_mode):
        raise Refusal("state_refused")
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise Refusal("state_refused")
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise Refusal("state_refused") from None


def prune_and_count(dirfd, now):
    """Drop long-expired claims under the caller's lock; count the rest."""
    try:
        names = os.listdir(dirfd)
    except OSError:
        raise Refusal("state_refused") from None
    live = 0
    for name in names:
        if not CLAIM_NAME_RE.fullmatch(name):
            continue
        expired = False
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
        except OSError:
            live += 1
            continue
        try:
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(MAX_CLAIM_BYTES + 1)
            if len(raw) <= MAX_CLAIM_BYTES:
                claim = json.loads(raw.decode("utf-8"))
                expires = claim.get("expires_at") if isinstance(claim, dict) else None
                expired = (type(expires) is int
                           and expires < now - PRUNE_AFTER_SECONDS)
        except (OSError, ValueError, UnicodeDecodeError):
            expired = False
        if expired:
            try:
                os.unlink(name, dir_fd=dirfd)
            except OSError:
                live += 1
        else:
            live += 1
    return live


def claim_request(dirfd, request_id, claim):
    name = request_id + ".claim"
    body = json.dumps(claim, sort_keys=True).encode("utf-8")
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=dirfd)
    except OSError as error:
        if error.errno == errno.EEXIST:
            raise Refusal("request_replayed") from None
        raise Refusal("state_refused") from None
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise Refusal("state_refused") from None


def write_receipt(dirfd, request_id, receipt):
    body = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        fd = os.open(request_id + ".receipt.json",
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=dirfd)
    except OSError:
        raise Refusal("state_refused") from None
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise Refusal("state_refused") from None


def child_environment():
    child = {key: os.environ[key] for key in CHILD_ENV_KEYS if key in os.environ}
    child.update(FIXED_CHILD_ENV)
    return child


def minimize_observations(observations, worker_version):
    """Recheck the two-GET ceiling; return (minimized, fatal code or None)."""
    if not isinstance(observations, list) or not 1 <= len(observations) <= 2:
        return [], "adapter_receipt_refused"
    minimized = []
    for index, record in enumerate(observations):
        if (not isinstance(record, dict) or set(record) - OBSERVATION_KEYS
                or record.get("surface") != OBSERVATION_ORDER[index]):
            return [], "adapter_receipt_refused"
        status = record.get("status")
        if type(status) is not int or not 100 <= status <= 599:
            return [], "adapter_receipt_refused"
        observed = record.get("worker_version")
        if not _shape(observed, UUID_RE):
            return [], "adapter_receipt_refused"
        ray = record.get("ray")
        if ray is not None and not _shape(ray, RAY_RE):
            return [], "adapter_receipt_refused"
        minimized.append({"surface": record["surface"], "status": status,
                          "worker_version_matched": observed == worker_version})
    if minimized[0]["status"] != 200 and len(minimized) != 1:
        return [], "adapter_receipt_refused"
    if not all(item["worker_version_matched"] for item in minimized):
        return minimized, "observed_worker_mismatch"
    return minimized, None


def minimize_receipt(report, manifest):
    """Validate the adapter report; return (outcome, failure, observations)."""
    if not isinstance(report, dict) or set(report) - RECEIPT_KEYS:
        return "fail", "adapter_receipt_refused", []
    result = report.get("result")
    if result not in ("pass", "fail"):
        return "fail", "adapter_receipt_refused", []
    failure = report.get("failure")
    if result == "pass":
        if failure is not None:
            return "fail", "adapter_receipt_refused", []
    elif not _shape(failure, FAILURE_RE):
        return "fail", "adapter_receipt_refused", []
    if "runtime_observations" not in report:
        return "fail", "adapter_receipt_refused", []
    observations, fatal = minimize_observations(
        report["runtime_observations"], manifest["targets"]["worker_version"])
    if fatal == "adapter_receipt_refused":
        return "fail", fatal, []
    control = report.get("control_plane")
    bound = {key: manifest["targets"][key] for key in TARGET_KEYS
             if key != "origin_sha256"}
    if result == "pass" and control is None:
        return "fail", "adapter_receipt_refused", []
    if control is not None and control != bound:
        return "fail", "control_plane_mismatch", observations
    if fatal is not None:
        return "fail", fatal, observations
    if result == "fail":
        return "fail", failure, observations
    return "pass", None, observations


def run_adapter(adapter_path, timeout):
    try:
        done = subprocess.run(
            [sys.executable, "-I", os.fspath(adapter_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, env=child_environment(),
            cwd="/")
    except subprocess.TimeoutExpired:
        return None, b"", "dispatch_timeout"
    # stderr is drained and discarded: remote text never reaches a receipt.
    if len(done.stderr) > MAX_STDERR_BYTES + 1 or len(done.stdout) > MAX_STDOUT_BYTES + 1:
        return done.returncode, b"", "adapter_output_over_cap"
    if done.returncode != 0:
        return done.returncode, b"", "dispatch_failed"
    return done.returncode, done.stdout, None


def dispatch(request_path, install, now=None, expected_uid=0,
             dispatch_timeout=DISPATCH_TIMEOUT_SECONDS):
    """Validate, claim one-shot state, run the fixed adapter, attest the receipt.

    Raises Refusal before any adapter I/O. Once the adapter has run, returns
    the minimized dispatch receipt with outcome pass or fail instead.
    """
    moment = int(time.time()) if now is None else now
    request_data, request_info = read_regular(
        request_path, MAX_REQUEST_BYTES, "request_unreadable", "request_too_large")
    if request_info.st_mode & 0o002:
        raise Refusal("request_unreadable")
    request = parse_request(request_data)
    if any(variable in os.environ for variable in PROXY_VARIABLES):
        raise Refusal("proxy_environment_refused")
    manifest_data, manifest_info = read_regular(
        install.manifest_path, MAX_MANIFEST_BYTES,
        "manifest_unreadable", "manifest_too_large")
    if manifest_info.st_uid != expected_uid or manifest_info.st_mode & 0o022:
        raise Refusal("manifest_ownership_refused")
    manifest = parse_manifest(manifest_data)
    if manifest["approval"]["expires_at"] <= moment:
        raise Refusal("approval_expired")
    if invoker() != manifest["principal"]["user"]:
        raise Refusal("principal_mismatch")
    if request["principal"] != manifest["principal"]["user"]:
        raise Refusal("principal_mismatch")
    if request["approval_id"] != manifest["approval"]["id"]:
        raise Refusal("authorization_refused")
    if request["expires_at"] <= moment:
        raise Refusal("request_expired")
    if request["expires_at"] > manifest["approval"]["expires_at"]:
        raise Refusal("request_expiry_beyond_approval")
    if request["targets"] != manifest["targets"]:
        raise Refusal("targets_refused")
    adapter_data, adapter_info = read_regular(
        install.adapter_path, MAX_ADAPTER_BYTES,
        "adapter_unreadable", "adapter_too_large")
    if adapter_info.st_uid != expected_uid or adapter_info.st_mode & 0o022:
        raise Refusal("adapter_ownership_refused")
    adapter_digest = hashlib.sha256(adapter_data).hexdigest()
    if adapter_digest != manifest["adapter_sha256"]:
        raise Refusal("adapter_digest_mismatch")

    dirfd = open_state_dir(install.state_dir)
    try:
        fcntl.flock(dirfd, fcntl.LOCK_EX)
        try:
            if prune_and_count(dirfd, moment) >= MAX_CLAIMS:
                raise Refusal("state_exhausted")
            claim_request(dirfd, request["request_id"], {
                "request_sha256": hashlib.sha256(request_data).hexdigest(),
                "approval_id": request["approval_id"],
                "expires_at": request["expires_at"],
                "claimed_at": moment, "pid": os.getpid()})
        finally:
            fcntl.flock(dirfd, fcntl.LOCK_UN)
        status, output, error = run_adapter(install.adapter_path, dispatch_timeout)
        if error is not None:
            outcome, failure, observations = "fail", error, []
        else:
            try:
                report = json.loads(output.decode("utf-8").strip())
            except (UnicodeDecodeError, ValueError):
                report = None
            if report is None:
                outcome, failure, observations = "fail", "adapter_receipt_refused", []
            else:
                outcome, failure, observations = minimize_receipt(report, manifest)
        receipt = {
            "kind": KIND_RECEIPT, "schema": SCHEMA_RECEIPT, "action": ACTION,
            "request_id": request["request_id"],
            "approval_id": request["approval_id"],
            "principal": manifest["principal"]["user"],
            "adapter_sha256": adapter_digest,
            "targets": manifest["targets"],
            "interval": {"claimed_at": moment, "finished_at": moment,
                         "expires_at": request["expires_at"]},
            "outcome": outcome, "failure": failure,
            "observations": observations, "adapter_exit": status,
        }
        write_receipt(dirfd, request["request_id"], receipt)
        return receipt
    finally:
        os.close(dirfd)


def main(argv=None, _install=None, _now=None, _uid=None):
    """Command entry: one request flag, fixed installed paths, exit mapping.

    The underscore seams exist for the offline suite only; no command-line
    flag reaches them.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    install = _install if _install is not None else production_install()
    expected = 0 if _uid is None else _uid
    try:
        receipt = dispatch(args.request, install, now=_now, expected_uid=expected)
    except Refusal as refused:
        print(f"REFUSED: {refused.code}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["outcome"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
