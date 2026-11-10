#!/usr/bin/env python3
"""Checked portable plugin transaction. No service or credential adapter is shipped."""

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid


MANIFEST_MAX_BYTES = 16384
INTERNAL_PATHS = {".cliproxy-plugin-owner", ".cliproxy-plugin.lock"}


class Refusal(Exception):
    pass


def require(condition):
    if not condition:
        raise Refusal("precondition or postcondition failed")


def regular(path):
    require(stat.S_ISREG(path.lstat().st_mode))
    return path


def digest(path):
    regular(path)
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def absent(path):
    return not os.path.lexists(path)


def secure_dir(path):
    require(path.is_absolute() and path == path.resolve())
    for item in (path, *path.parents):
        info = item.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, os.geteuid()))
        # Only root-owned sticky ancestors, never a mutable target, may be shared.
        shared_ancestor = item != path and info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        require(not info.st_mode & 0o022 or shared_ancestor)
    require(path.stat().st_uid == os.geteuid())


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, data, mode=0o600, exclusive=False):
    fd, name = tempfile.mkstemp(prefix=".transaction-", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            # Publish only complete bytes without replacing any existing destination.
            os.link(tmp, path)
        else:
            os.replace(tmp, path)
        sync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def command(*args):
    # Do not expose command output: it may contain configuration or credentials.
    subprocess.run(args, check=True, timeout=200, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


class Deployment:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        secure_dir(self.state_dir)
        require(stat.S_IMODE(self.state_dir.stat().st_mode) == 0o700)
        self.plan = None

    def state(self, value):
        atomic(self.state_dir / "state", (value + "\n").encode())
        print("STATE " + value, flush=True)

    def read_state(self):
        path = self.state_dir / "state"
        return regular(path).read_text().strip() if not absent(path) else ""

    def target(self, relative):
        require(isinstance(relative, str) and relative and not Path(relative).is_absolute())
        path = self.root / relative
        require(path == path.resolve(strict=False) and path.is_relative_to(self.root))
        require(path != self.root and path.relative_to(self.root).parts[0] not in INTERNAL_PATHS)
        secure_dir(path.parent)
        return path

    def helper(self, action):
        command(self.plan["adapter"], action, str(self.state_dir))

    def healthy(self):
        try:
            self.helper("models-baseline")
            self.helper("route")
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    @contextlib.contextmanager
    def lock(self, directory):
        secure_dir(directory)
        fd = os.open(directory / ".cliproxy-plugin.lock",
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid())
            require(stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def load(self):
        path = self.state_dir / "deployment.json"
        require(regular(path).stat().st_size <= MANIFEST_MAX_BYTES)
        self.plan = json.loads(path.read_text())
        require(self.plan["schema"] == 1)
        require(re.fullmatch(r"[a-f0-9]{32}", self.plan["id"]) is not None)
        self.root = Path(self.plan["root"])
        secure_dir(self.root)
        self.config = self.target("config.yaml")
        self.auth = self.target(self.plan["auth"])
        self.artifact = self.target(self.plan["artifact"])
        require(len({self.config, self.auth, self.artifact}) == 3)
        require(not self.state_dir.is_relative_to(self.root))
        require(Path(self.plan["adapter"]).is_absolute())

    def ownership(self):
        marker = self.root / ".cliproxy-plugin-owner"
        expected = self.plan["id"] + " " + digest(self.state_dir / "deployment.json") + "\n"
        require(regular(marker).read_text() == expected)
        for name, key in (("config.yaml.orig", "original"), ("config.yaml.new", "rendered")):
            require(digest(self.state_dir / name) == self.plan[key])
        # Refuse the entire rollback before mutation if any affected identity drifted.
        require(digest(self.config) in (self.plan["original"], self.plan["rendered"]))
        for path, key in ((self.auth, "auth_sha256"), (self.artifact, "artifact_sha256")):
            require(absent(path) or digest(path) == self.plan[key])

    def config_metadata(self):
        info = regular(self.config).stat()
        parent = self.root.stat()
        group = parent.st_gid if parent.st_mode & stat.S_ISGID else os.getegid()
        # Atomic replacements use the invoking owner and the directory's creation group.
        require(stat.S_IMODE(info.st_mode) == 0o600)
        require(info.st_uid == os.geteuid() and info.st_gid == group)

    def postconditions(self):
        require(digest(self.config) == self.plan["original"])
        self.config_metadata()
        require(absent(self.auth) and absent(self.artifact))

    def restore_config(self):
        fd, name = tempfile.mkstemp(prefix=".restore-", dir=self.root)
        os.close(fd)
        tmp = Path(name)
        try:
            command("cp", "--", str(self.state_dir / "config.yaml.orig"), str(tmp))
            command("chmod", "0600", "--", str(tmp))
            require(digest(tmp) == self.plan["original"])
            require(stat.S_IMODE(tmp.stat().st_mode) == 0o600)
            with tmp.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(tmp, self.config)
            sync_dir(self.root)
        finally:
            tmp.unlink(missing_ok=True)

    def rollback_locked(self):
        try:
            self.ownership()
            self.state("rolling-back")
            # Every command must succeed even when a later health check is green.
            command("rm", "-f", "--", str(self.auth))
            require(absent(self.auth))
            self.restore_config()
            command("rm", "-f", "--", str(self.artifact))
            self.postconditions()
            if not self.healthy():
                self.helper("restart")
                require(self.healthy())
            # Recheck after helpers, including on a repeated rollback.
            self.ownership()
            self.postconditions()
            sync_dir(self.auth.parent)
            sync_dir(self.artifact.parent)
            self.state("rolledback")
            return 0
        except (OSError, ValueError, KeyError, Refusal, subprocess.SubprocessError):
            try:
                self.state("rollback-FAILED")
            except OSError:
                pass
            print("Rollback incomplete; retain state and reconcile affected identities.", file=sys.stderr)
            return 1

    def rollback(self):
        with self.lock(self.state_dir):
            self.load()
            with self.lock(self.root):
                return self.rollback_locked()

    def prepare(self):
        self.root = Path(os.environ["S"])
        secure_dir(self.root)
        require(not self.state_dir.is_relative_to(self.root))
        self.config = self.target("config.yaml")
        self.auth = self.target(os.environ["AUTH_REL"])
        self.artifact = self.target(os.environ["ARTIFACT_REL"])
        require(len({self.config, self.auth, self.artifact}) == 3)
        require(absent(self.auth) and absent(self.artifact))
        require(absent(self.state_dir / "deployment.json"))
        adapter = Path(os.environ["DEPLOY_ADAPTER"])
        require(adapter.is_absolute() and os.access(regular(adapter), os.X_OK))
        source = Path(os.environ["ARTIFACT_SRC"])
        sha = os.environ["SO_SHA"]
        require(re.fullmatch(r"[a-f0-9]{64}", sha) is not None and digest(source) == sha)
        self.plan = {"schema": 1, "id": uuid.uuid4().hex, "root": str(self.root),
                     "adapter": str(adapter), "auth": str(self.auth.relative_to(self.root)),
                     "artifact": str(self.artifact.relative_to(self.root)),
                     "artifact_sha256": sha, "original": digest(self.config)}
        self.helper("preflight")
        require(self.healthy())
        self.config_metadata()
        original = regular(self.config).read_bytes()
        require(hashlib.sha256(original).hexdigest() == self.plan["original"])
        atomic(self.state_dir / "config.yaml.orig", original)
        require(digest(self.state_dir / "config.yaml.orig") == self.plan["original"])
        self.helper("render")
        rendered = self.state_dir / "config.yaml.new"
        require(stat.S_IMODE(regular(rendered).stat().st_mode) == 0o600)
        with rendered.open("rb") as stream:
            os.fsync(stream.fileno())
        self.plan["rendered"] = digest(rendered)
        require(self.plan["rendered"] != self.plan["original"])
        # Adapter returns one auth record over a private pipe, never argv or logs.
        auth = subprocess.run([str(adapter), "auth", str(self.state_dir)], check=True,
                              timeout=200, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL).stdout
        require(isinstance(json.loads(auth), dict))
        self.plan["auth_sha256"] = hashlib.sha256(auth).hexdigest()
        manifest = json.dumps(self.plan, sort_keys=True).encode()
        require(len(manifest) <= MANIFEST_MAX_BYTES)
        atomic(self.state_dir / "deployment.json", manifest)
        marker = self.plan["id"] + " " + digest(self.state_dir / "deployment.json") + "\n"
        atomic(self.root / ".cliproxy-plugin-owner", marker.encode())
        return source, auth

    def start_watchdog(self):
        delay = int(os.environ.get("WD_SECS", "600"))
        require(0 <= delay <= 86400)
        fd = os.open(self.state_dir / "watchdog.log",
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "watchdog"],
                             env={"PATH": os.defpath, "STATE_DIR": str(self.state_dir),
                                  "WD_SECS": str(delay)}, start_new_session=True, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=fd, stderr=fd)
        finally:
            os.close(fd)

    def run(self):
        self.root = Path(os.environ["S"])
        secure_dir(self.root)
        # Always acquire state before service, including the recovery entry points.
        with self.lock(self.state_dir), self.lock(self.root):
            # Never rewrite the state/backup of an already prepared transaction.
            require(absent(self.state_dir / "deployment.json"))
            try:
                self.state("preflight")
                source, auth = self.prepare()
            except (OSError, ValueError, KeyError, Refusal, subprocess.SubprocessError):
                self.state("aborted-unchanged")
                return 3
            try:
                self.start_watchdog()
                self.state("applying")
                self.ownership()
                # Exclusive publication never exposes partial plugin/auth material.
                for path, data, mode in ((self.artifact, source.read_bytes(), 0o755),
                                         (self.auth, auth, 0o600)):
                    atomic(path, data, mode, exclusive=True)
                self.ownership()
                atomic(self.config, (self.state_dir / "config.yaml.new").read_bytes())
                self.state("loading")
                self.helper("verify")
                self.ownership()
                require(digest(self.config) == self.plan["rendered"])
                require(digest(self.auth) == self.plan["auth_sha256"])
                require(digest(self.artifact) == self.plan["artifact_sha256"])
                self.config_metadata()
                self.state("verified")
                return 0
            except (OSError, ValueError, KeyError, Refusal, subprocess.SubprocessError):
                self.rollback_locked()
                return 1

    def watchdog(self):
        delay = int(os.environ.get("WD_SECS", "600"))
        require(0 <= delay <= 86400)
        time.sleep(delay)
        with self.lock(self.state_dir):
            self.load()
            with self.lock(self.root):
                current = self.read_state()
                if current in ("aborted-unchanged", "rolledback"):
                    # Historical completion must not mutate a newer deployment.
                    return 0
                if current == "rollback-FAILED":
                    return 1
                if current == "verified":
                    self.ownership()
                    require(digest(self.config) == self.plan["rendered"])
                    require(digest(self.auth) == self.plan["auth_sha256"])
                    require(digest(self.artifact) == self.plan["artifact_sha256"])
                    return 0
                return self.rollback_locked()


def main(argv):
    if len(argv) != 1 or argv[0] not in ("run", "rollback", "watchdog"):
        print("usage: cliproxy_plugin_deploy.sh run|rollback|watchdog", file=sys.stderr)
        return 2
    try:
        deployment = Deployment(os.environ["STATE_DIR"])
        return getattr(deployment, argv[0])()
    except BlockingIOError:
        print("Transaction busy; no completion claimed. Retry after the holder exits.", file=sys.stderr)
        return 75
    except (OSError, ValueError, KeyError, TypeError, Refusal, subprocess.SubprocessError):
        print("Transaction refused or incomplete; inspect private state without publishing it.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
