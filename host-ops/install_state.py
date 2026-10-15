#!/usr/bin/env python3
"""Root-only, fixed-file installer journal. Never archives keys, audit or spool.

The library's root/owner arguments exist for offline filesystem fixtures. The
operator CLI always uses the real root and UID 0; it has no fixture/env bypass.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile

UNIT = "paperclip-host-ops-broker.service"
JOURNAL_ROOT = Path("/var/lib/paperclip-host-ops-installer")
PUBLIC_OUT = "/etc/paperclip-host-ops/response-signing.pub.pem"
# Source selector, installed path. New runtime files must be listed explicitly.
FILES = {
    "broker.py": "/usr/local/libexec/paperclip-host-ops/broker.py",
    "image_binding.py": "/usr/local/libexec/paperclip-host-ops/image_binding.py",
    "RUNBOOK.md": "/usr/local/share/doc/paperclip-host-ops/RUNBOOK.md",
    "host-ops-broker.service": "/etc/systemd/system/" + UNIT,
    "host-ops-broker.tmpfiles": "/usr/lib/tmpfiles.d/paperclip-host-ops.conf",
    "sudoers": "/etc/sudoers.d/paperclip-host-ops-broker",
    "sudoers-mirror": "/etc/paperclip-host-ops/sudoers.rendered",
    "config": "/etc/paperclip-host-ops/config.json",
    "public-key": PUBLIC_OUT,
}
KEYS = (
    "/var/lib/paperclip-host-ops/keys/response-signing.pem",
    "/var/lib/paperclip-host-ops/keys/response-signing.pub.pem",
)
MAX_FILE = 2 * 1024 * 1024
MAX_JOURNAL = 32 * 1024 * 1024


class Refusal(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def metadata(info):
    return {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid,
            "atimeNs": info.st_atime_ns, "mtimeNs": info.st_mtime_ns}


def check_key_identity(info, saved):
    # Detect replacement and ordinary in-place writes without reading key bytes.
    # This is not a content attestation against a writer that can forge mtime.
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    ) != (saved["device"], saved["inode"], saved["size"], saved["mtimeNs"]):
        raise Refusal("signing-key identity or modification metadata changed")


def regular(path, limit=MAX_FILE):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise Refusal("managed file is not a bounded single-link regular file")
        chunks = []
        size = 0
        while True:
            part = os.read(fd, min(65536, limit + 1 - size))
            if not part:
                break
            chunks.append(part)
            size += len(part)
            if size > limit:
                raise Refusal("managed file exceeds size bound")
        after = os.fstat(fd)
        if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise Refusal("managed file changed while capturing preimage")
        attrs = {key: base64.b64encode(os.getxattr(fd, key)).decode() for key in os.listxattr(fd)}
        return b"".join(chunks), before, attrs
    finally:
        os.close(fd)


class InstallState:
    def __init__(self, directory, root=Path("/"), owner=0):
        self.directory = Path(directory)
        self.root = Path(root)
        self.owner = owner

    def target(self, absolute):
        if absolute not in set(FILES.values()) | set(KEYS):
            raise Refusal("journal target is not in the fixed install inventory")
        return self.root / absolute.lstrip("/")

    def key_owners(self):
        owners = {self.owner}
        try:
            owners.add(pwd.getpwnam("paperclip-host-reader").pw_uid)
        except KeyError:
            pass  # Bootstrap preimages may still be root-owned.
        return owners

    def parents(self, path, missing=False, key_path=False):
        # Every parent must be trusted, non-writable by other principals and not
        # a symlink. Production stops at /; the fixture stops at its private root.
        key_owners = self.key_owners() if key_path else {self.owner}
        relative = path.parent.relative_to(self.root)
        current = self.root
        for part in (None, *relative.parts):
            if part is not None:
                current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if missing:
                    return
                raise
            allowed = {self.owner}
            if key_path and current in {self.root / "var/lib/paperclip-host-ops", self.root / "var/lib/paperclip-host-ops/keys"}:
                allowed.update(key_owners)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in allowed or info.st_mode & 0o022:
                raise Refusal("untrusted install/journal parent")

    def validate_directory(self):
        self.parents(self.directory)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != self.owner or stat.S_IMODE(info.st_mode) != 0o700:
            raise Refusal("journal must be a trusted mode-0700 directory")

    def save(self, record, exclusive=False):
        self.validate_directory()
        dest = self.directory / "state.json"
        if exclusive and os.path.lexists(dest):
            raise Refusal("install journal already exists")
        payload = (json.dumps(record, sort_keys=True) + "\n").encode()
        if len(payload) > MAX_JOURNAL:
            raise Refusal("install journal exceeds size bound")
        fd, name = tempfile.mkstemp(prefix=".state-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if exclusive:
                os.link(name, dest, follow_symlinks=False)
                os.unlink(name)
            else:
                os.replace(name, dest)
            self.sync(self.directory)
        finally:
            if os.path.lexists(name):
                os.unlink(name)

    @staticmethod
    def sync(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def snapshot(self, sources, source_ref, service):
        if not re.fullmatch(r"[0-9a-f]{40}", source_ref) or set(sources) != set(FILES):
            raise Refusal("snapshot needs the exact source inventory and full commit")
        record = {"version": 1, "phase": "prepared", "sourceRef": source_ref,
                  "service": service, "files": {}, "keyMetadata": {}}
        for name, absolute in FILES.items():
            new, _, _ = regular(Path(sources[name]))
            path = self.target(absolute)
            self.parents(path, missing=True)
            before = None
            if os.path.lexists(path):
                old, info, attrs = regular(path)
                if info.st_uid != self.owner or info.st_mode & 0o022:
                    raise Refusal("managed preimage is not trusted")
                before = {**metadata(info), "sha256": digest(old),
                          "content": base64.b64encode(old).decode(), "xattrs": attrs}
            record["files"][absolute] = {"before": before, "afterSha256": digest(new)}
        # Metadata only: never read/copy the host-held signing private key here.
        for absolute in KEYS:
            path = self.target(absolute)
            self.parents(path, key_path=True)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Refusal("signing-key metadata target is not regular")
            if info.st_uid not in self.key_owners() or info.st_mode & 0o7022 or (
                absolute == KEYS[0] and info.st_mode & 0o077
            ):
                raise Refusal("signing-key preimage has unsafe ownership or mode")
            record["keyMetadata"][absolute] = {**metadata(info), "device": info.st_dev, "inode": info.st_ino, "size": info.st_size}
        self.validate(record)
        self.save(record, exclusive=True)
        return record

    @staticmethod
    def validate(record):
        if not isinstance(record, dict) or set(record) != {"version", "phase", "sourceRef", "service", "files", "keyMetadata"}:
            raise Refusal("invalid install journal shape")
        if record["version"] != 1 or record["phase"] not in {"prepared", "installed", "rolled_back"}:
            raise Refusal("invalid install journal version or phase")
        if not isinstance(record["sourceRef"], str) or not re.fullmatch(r"[0-9a-f]{40}", record["sourceRef"]):
            raise Refusal("journal source ref is not pinned")
        service = record["service"]
        if not isinstance(service, dict) or set(service) != {"active", "enabled"} or service["active"] not in {"active", "inactive"} or service["enabled"] not in {"enabled", "disabled", "absent"}:
            raise Refusal("unsupported prior service state")
        if service["enabled"] == "absent" and service["active"] != "inactive":
            raise Refusal("absent service cannot be active")
        if not isinstance(record["files"], dict) or set(record["files"]) != set(FILES.values()):
            raise Refusal("journal file inventory differs from fixed install inventory")
        for entry in record["files"].values():
            if not isinstance(entry, dict) or set(entry) != {"before", "afterSha256"} or not re.fullmatch(r"[0-9a-f]{64}", str(entry["afterSha256"])):
                raise Refusal("invalid managed-file record")
            before = entry["before"]
            if before is not None:
                if not isinstance(before, dict) or set(before) != {"mode", "uid", "gid", "atimeNs", "mtimeNs", "sha256", "content", "xattrs"}:
                    raise Refusal("invalid preimage record")
                InstallState.validate_metadata(before)
                try:
                    data = base64.b64decode(before["content"], validate=True)
                    if not isinstance(before["xattrs"], dict):
                        raise ValueError()
                    for key, value in before["xattrs"].items():
                        if not isinstance(key, str) or "\0" in key:
                            raise ValueError()
                        base64.b64decode(value, validate=True)
                except (TypeError, ValueError):
                    raise Refusal("invalid preimage encoding") from None
                if len(data) > MAX_FILE or digest(data) != before["sha256"]:
                    raise Refusal("preimage digest differs from captured bytes")
        keys = record["keyMetadata"]
        if not isinstance(keys, dict) or set(keys) != set(KEYS):
            raise Refusal("journal signing-key metadata inventory differs")
        for value in keys.values():
            if not isinstance(value, dict) or set(value) != {"mode", "uid", "gid", "atimeNs", "mtimeNs", "device", "inode", "size"}:
                raise Refusal("invalid signing-key metadata record")
            InstallState.validate_metadata(value)
            if any(type(value[k]) is not int or value[k] < 0 for k in ("device", "inode", "size")):
                raise Refusal("invalid signing-key identity")

    @staticmethod
    def validate_metadata(value):
        if any(type(value[k]) is not int or value[k] < 0 for k in ("mode", "uid", "gid", "atimeNs", "mtimeNs")) or value["mode"] > 0o7777:
            raise Refusal("invalid saved metadata")

    def load(self):
        self.validate_directory()
        data, info, _ = regular(self.directory / "state.json", MAX_JOURNAL)
        if info.st_uid != self.owner or stat.S_IMODE(info.st_mode) != 0o600:
            raise Refusal("journal file must be trusted mode 0600")
        record = json.loads(data)
        self.validate(record)
        return record

    def check_restore(self, record):
        self.validate(record)
        for absolute, entry in record["files"].items():
            path = self.target(absolute)
            self.parents(path, missing=True)
            if os.path.lexists(path):
                data, info, _ = regular(path)
                accepted = {entry["afterSha256"]}
                if entry["before"] is not None:
                    accepted.add(entry["before"]["sha256"])
                if info.st_uid != self.owner or info.st_mode & 0o022 or digest(data) not in accepted:
                    raise Refusal("managed target changed independently; refusing rollback overwrite")
        for absolute, saved in record["keyMetadata"].items():
            path = self.target(absolute)
            self.parents(path, key_path=True)
            info = path.lstat()
            check_key_identity(info, saved)

    def restore_files(self, record):
        # Preflight the complete inventory before the first restore/unlink.
        self.check_restore(record)
        for absolute, entry in record["files"].items():
            path = self.target(absolute)
            before = entry["before"]
            if before is None:
                if os.path.lexists(path):
                    self.parents(path)
                    path.unlink()
                    self.sync(path.parent)
                continue
            self.parents(path)
            fd, name = tempfile.mkstemp(prefix=".host-ops-restore-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(base64.b64decode(before["content"], validate=True))
                    stream.flush()
                    os.fchown(stream.fileno(), before["uid"], before["gid"])
                    os.fchmod(stream.fileno(), before["mode"])
                    for key in set(os.listxattr(stream.fileno())) - set(before["xattrs"]):
                        os.removexattr(stream.fileno(), key)
                    for key, value in before["xattrs"].items():
                        os.setxattr(stream.fileno(), key, base64.b64decode(value, validate=True))
                    os.utime(stream.fileno(), ns=(before["atimeNs"], before["mtimeNs"]))
                    os.fsync(stream.fileno())
                os.replace(name, path)
                self.sync(path.parent)
            finally:
                if os.path.lexists(name):
                    os.unlink(name)
        for absolute, saved in record["keyMetadata"].items():
            self.parents(self.target(absolute), key_path=True)
            fd = os.open(self.target(absolute), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                check_key_identity(info, saved)
                os.fchown(fd, saved["uid"], saved["gid"])
                os.fchmod(fd, saved["mode"])
                os.fsync(fd)
            finally:
                os.close(fd)
        self.verify_files(record)

    def verify_files(self, record):
        self.check_restore(record)
        for absolute, entry in record["files"].items():
            path = self.target(absolute)
            before = entry["before"]
            if before is None:
                if os.path.lexists(path):
                    raise Refusal("new install file survived rollback")
                continue
            data, info, attrs = regular(path)
            actual = metadata(info)
            if digest(data) != before["sha256"] or attrs != before["xattrs"] or any(
                actual[key] != before[key] for key in ("mode", "uid", "gid", "mtimeNs")
            ):
                raise Refusal("restored file does not match preimage")
        for absolute, saved in record["keyMetadata"].items():
            info = self.target(absolute).lstat()
            check_key_identity(info, saved)
            if any(metadata(info)[key] != saved[key] for key in ("mode", "uid", "gid")):
                raise Refusal("signing-key identity or metadata was not restored")


def systemctl(*args, allow_failure=False):
    try:
        result = subprocess.run(["/usr/bin/systemctl", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, timeout=45,
                                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"})
    except (OSError, subprocess.SubprocessError):
        raise Refusal("service state operation failed or timed out") from None
    if result.returncode and not allow_failure:
        raise Refusal("service state operation failed")
    return result.returncode, result.stdout


def service_state(require_stable=True):
    # v255 show suppresses empty values without --all, including the absent
    # unit's UnitFileState. See systemd/systemd v255 man/systemctl.xml.
    _, output = systemctl("show", UNIT, "--all", "--no-pager", "--property=LoadState,ActiveState,UnitFileState")
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if set(fields) != {"LoadState", "ActiveState", "UnitFileState"}:
        raise Refusal("incomplete service state observation")
    enabled = fields["UnitFileState"]
    if fields["LoadState"] == "not-found" and fields["ActiveState"] == "inactive" and not enabled:
        enabled = "absent"
    elif fields["LoadState"] != "loaded" or enabled not in {"enabled", "disabled"}:
        raise Refusal("masked/static/transient service state requires separate review")
    state = {"active": fields["ActiveState"], "enabled": enabled}
    allowed = {"active", "inactive"} if require_stable else {"active", "inactive", "failed", "activating", "deactivating", "reloading"}
    if state["active"] not in allowed:
        raise Refusal("changing or failed service state requires separate review")
    return state


def mark_installed(journal):
    record = journal.load()
    if record["phase"] != "prepared":
        raise Refusal("journal cannot be marked installed from this phase")
    journal.check_restore(record)
    for absolute, entry in record["files"].items():
        data, _, _ = regular(journal.target(absolute))
        if digest(data) != entry["afterSha256"]:
            raise Refusal("installed file differs from planned bytes")
    if service_state() != {"active": "active", "enabled": "enabled"}:
        raise Refusal("installed service is not active and enabled")
    record["phase"] = "installed"
    journal.save(record)


def rollback(journal):
    record = journal.load()
    if record["phase"] == "rolled_back":
        journal.verify_files(record)
        if service_state() != record["service"]:
            raise Refusal("previously restored service state changed")
        return
    journal.check_restore(record)
    current = service_state(require_stable=False)
    if current["enabled"] != "absent":
        systemctl("stop", UNIT)
        if service_state(require_stable=False)["active"] != "inactive":
            raise Refusal("service did not become inactive before restore")
        if record["service"]["enabled"] != "enabled":
            systemctl("disable", UNIT)
    journal.restore_files(record)
    systemctl("daemon-reload")
    if record["service"]["enabled"] == "enabled":
        systemctl("enable", UNIT)
    if record["service"]["active"] == "active":
        systemctl("start", UNIT)
    if service_state() != record["service"]:
        raise Refusal("restored service state does not match preimage")
    journal.verify_files(record)
    record["phase"] = "rolled_back"
    journal.save(record)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("snapshot", "installed", "rollback"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--source-ref")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--sudoers", type=Path)
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise Refusal("installer journal requires root")
        if args.state.parent != JOURNAL_ROOT or not re.fullmatch(r"install-[A-Za-z0-9]+", args.state.name):
            raise Refusal("journal must be a direct install-* child of the protected journal root")
        journal = InstallState(args.state)
        if args.command == "snapshot":
            if any(value is None for value in (args.source_dir, args.source_ref, args.config, args.sudoers)):
                raise Refusal("snapshot source arguments are required")
            sources = {name: args.source_dir / name for name in FILES}
            sources.update({"config": args.config, "sudoers": args.sudoers,
                            "sudoers-mirror": args.sudoers, "public-key": Path(KEYS[1])})
            journal.snapshot(sources, args.source_ref, service_state())
        elif args.command == "installed":
            mark_installed(journal)
        else:
            rollback(journal)
    except (Refusal, OSError, ValueError, TypeError) as exc:
        message = str(exc) if isinstance(exc, Refusal) else "install journal operation failed"
        print("REFUSED: " + message, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
