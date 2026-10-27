"""Offline test utilities only. Never invoke an installed wrapper or run as root."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import select
import shutil
import stat
import subprocess


BASELINE_SHA = "30b9f2ff45d2cc54fa8cb52da7da7436ef49029e789d690c528a847035f30e6d"
REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests/fixtures/op-request-known-bad.sh"
SOURCE = REPO / "ops/operator-safety/wrappers/op-request"


class Refused(RuntimeError):
    """Harness/setup failure, not a passing product refusal."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def regular_bytes(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise Refused(f"not a regular non-symlink file: {path}")
    return path.read_bytes()


@dataclass(frozen=True)
class Provenance:
    path: Path
    kind: str
    data: bytes

    @classmethod
    def load(cls, path, kind):
        data = regular_bytes(path)
        if kind == "truthful":
            expected = BASELINE_SHA
        elif kind == "fixed":
            manifest = regular_bytes(path.with_name("op-request.sha256"))
            match = re.fullmatch(rb"([0-9a-f]{64})  op-request\n", manifest)
            if not match:
                raise Refused("manifest must be exactly: 64 lowercase hex, two spaces, op-request, newline")
            expected = match[1].decode("ascii")
        else:
            raise Refused(f"unknown source kind: {kind}")
        if digest(data) != expected:
            raise Refused("source/hash mismatch (wrapper was not executed)")
        return cls(path, kind, data)

    def verify(self):
        current = self.load(self.path, self.kind)
        if current.data != self.data:
            raise Refused("source changed during the suite")


def scratch_root():
    # CI without a run-owned directory must supply TMPDIR explicitly.
    raw = (os.environ.get("PAPERCLIP_RUN_SCRATCH_DIR")
           or os.environ.get("PAPERCLIP_SCRATCH_DIR") or os.environ.get("TMPDIR"))
    if not raw or not Path(raw).is_absolute():
        raise Refused("set a real absolute run scratch directory or TMPDIR")
    root = Path(raw).resolve(strict=True)
    if not root.is_dir():
        raise Refused("scratch parent is not a directory")
    return root


@contextmanager
def sandbox():
    if os.geteuid() == 0:
        raise Refused("offline suites must not run as root")
    root = scratch_root()
    made = subprocess.run(
        ["/usr/bin/mktemp", "-d", "--", str(root / "opreq.XXXXXXXX")],
        capture_output=True, text=True, timeout=10, check=False,
    )
    # Validate status AND the entire returned path before mkdir/write/chmod or
    # cleanup. Never turn an empty/invalid T into /bin (including in cleanup).
    if made.returncode != 0:
        raise Refused("mktemp failed; no setup or wrapper execution")
    if not re.fullmatch(re.escape(str(root)) + r"/opreq\.[A-Za-z0-9]{8}\n", made.stdout):
        raise Refused("invalid mktemp path; no setup or wrapper execution")
    path = Path(made.stdout[:-1])
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or path.resolve(strict=True) != path
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
            or any(path.iterdir())):
        raise Refused("mktemp directory is not private, owned, real, and empty")
    try:
        yield path
    finally:
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise Refused("sandbox identity changed; refusing cleanup")
        shutil.rmtree(path)


def write_new(path, data):
    with path.open("xb") as stream:
        if stream.write(data) != len(data):
            raise Refused(f"short setup write: {path.name}")
    if regular_bytes(path) != data:
        raise Refused(f"setup write mismatch: {path.name}")


def assignment_indices(data, replacements):
    lines = data.splitlines(keepends=True)
    indices = {}
    for name in replacements:
        found = [i for i, line in enumerate(lines) if line.startswith(name.encode() + b"=")]
        if len(found) != 1:
            raise Refused(f"expected exactly one {name} assignment; found {len(found)}")
        indices[name] = found[0]
    return lines, indices


def rewrite(data, replacements):
    lines, indices = assignment_indices(data, replacements)
    for name, index in indices.items():
        lines[index] = f"{name}={replacements[name]}\n".encode()
    return b"".join(lines)


def verify_rewrite(original, candidate, replacements):
    # Independent comparison of EVERY line. An empty diff, a failed write,
    # duplicate/missing assignment, or an unrelated edit is never a pass.
    before, indices = assignment_indices(original, replacements)
    after, _ = assignment_indices(candidate, replacements)
    if len(before) != len(after):
        raise Refused("rewrite changed the line count")
    allowed = {index: f"{name}={replacements[name]}\n".encode() for name, index in indices.items()}
    for index, (old, new) in enumerate(zip(before, after)):
        if index in allowed:
            if new != allowed[index] or new == old:
                raise Refused("missing, failed, or incorrect path rewrite")
        elif new != old:
            raise Refused("runner differs outside approved rewrites")


def shell_literal(path):
    return "'" + str(path).replace("'", "'\"'\"'") + "'"


def install_fakes(path, names):
    bin_dir = path / "bin"
    bin_dir.mkdir(mode=0o700)
    data = regular_bytes(Path(__file__).with_name("op_request_fake_command.py"))
    for name in names:
        command = bin_dir / name
        write_new(command, data)
        command.chmod(0o700)
    return bin_dir


def clean_env(path, **extra):
    # No inherited credentials, BASH_ENV, exported functions, or real accounts.
    return {"PATH": f"{path}/bin:/usr/bin:/bin", "HOME": str(path),
            "LC_ALL": "C", "OPREQ_SANDBOX": str(path), **extra}


def read_ready(fd):
    if not select.select([fd], [], [], 10)[0] or os.read(fd, 1) != b"1":
        raise Refused("publication/output-fault handshake failed")


def run_process(command, env, broken_stdout=False):
    if not broken_stdout:
        return subprocess.run(command, env=env, capture_output=True, text=True, timeout=15, check=False)
    # The real validation write must finish. The fake final chmod then pauses
    # after publication while the parent closes the ONLY stdout reader. Release
    # it before the real audit append/final stdout. No sleeps or pipe race.
    out_r, out_w = os.pipe()
    ready_r, ready_w = os.pipe()
    release_r, release_w = os.pipe()
    fds = {out_r, out_w, ready_r, ready_w, release_r, release_w}
    proc = None

    def close(fd):
        os.close(fd)
        fds.remove(fd)

    try:
        env = {**env, "OPREQ_READY_FD": str(ready_w), "OPREQ_RELEASE_FD": str(release_r)}
        proc = subprocess.Popen(command, env=env, stdout=out_w, stderr=subprocess.PIPE,
                                pass_fds=(ready_w, release_r), text=True)
        for fd in (out_w, ready_w, release_r):
            close(fd)
        read_ready(ready_r)
        if not select.select([out_r], [], [], 5)[0]:
            raise Refused("validation stdout was not observed before EPIPE")
        validated = os.read(out_r, 4096)
        if validated != b"request-ok\n":
            raise Refused(f"unexpected pre-publication stdout: {validated!r}")
        close(out_r)
        if os.write(release_w, b"1") != 1:
            raise Refused("cannot release final-output fault")
        close(release_w)
        _, stderr = proc.communicate(timeout=15)
        return subprocess.CompletedProcess(command, proc.returncode, validated.decode(), stderr)
    finally:
        for fd in fds:
            os.close(fd)
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)


class PublicationCase:
    def __init__(self, path, source, fault):
        self.path, self.source, self.fault = path, source, fault
        source.verify()
        self.queue = path / "queue"
        self.queue.mkdir(mode=0o700)
        self.log = path / "operator-actions.log"
        self.faults = set(fault.split(",")) if fault else set()
        if "log" in self.faults:
            self.log.mkdir(mode=0o700)  # append fails even without root/chmod tricks
        else:
            write_new(self.log, b"")
        install_fakes(path, ("id", "cp", "chown", "chmod"))
        self.src = path / "src.json"
        write_new(self.src, b'{"what":"offline dummy","why":"test only","rollback":"discard dummy"}\n')
        self.replacements = {"LOG": shell_literal(self.log), "QUEUE": shell_literal(self.queue)}
        self.runner = path / "run.sh"
        candidate = rewrite(source.data, self.replacements)
        verify_rewrite(source.data, candidate, self.replacements)
        write_new(self.runner, candidate)

    def run(self, invalid=False):
        src = self.src
        if invalid:
            src = self.path / "invalid.json"
            write_new(src, b'{"what":"x","why":"missing rollback"}\n')
        # EVERY invocation, including log/EPIPE/validation failure, is gated.
        self.source.verify()
        verify_rewrite(self.source.data, regular_bytes(self.runner), self.replacements)
        env = clean_env(self.path, OPREQ_MODE="publication", OPREQ_FAULT=self.fault)
        return run_process(["/bin/bash", str(self.runner), str(src)], env,
                           broken_stdout="stdout" in self.faults)

    def files(self):
        return list(self.queue.iterdir())

    def audit(self):
        return "" if self.log.is_dir() else self.log.read_text()

    def events(self):
        import json
        path = self.path / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@contextmanager
def publication_case(source, fault=""):
    source.verify()  # before even allocating scratch
    with sandbox() as path:
        yield PublicationCase(path, source, fault)
