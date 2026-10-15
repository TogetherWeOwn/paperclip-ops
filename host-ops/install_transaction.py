#!/usr/bin/env python3
"""Locked, journaled upgrades of an already provisioned host-ops installation.

No bootstrap, account creation, directory chmod, key rotation or carrier changes.
CLI has no fake-root/environment bypass; fixtures call library functions directly.
"""

import argparse
from contextlib import contextmanager
import ctypes
import fcntl
import grp
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import signal
import stat
import sys
import tempfile

sys.dont_write_bytecode = True
import install_state as state
import spool_provisioning as provisioning

RUNTIME_DIRS = {
    "/var/lib/paperclip-host-ops": (0o750, "reader"),
    "/var/lib/paperclip-host-ops/keys": (0o700, "reader"),
    "/var/log/paperclip-host-ops": (0o750, "reader"),
    "/var/spool/paperclip-host-ops": (0o750, "requests"),
    "/var/spool/paperclip-host-ops/requests": (0o3730, "requests"),
    "/var/spool/paperclip-host-ops/previews": (0o2750, "requests"),
    "/var/spool/paperclip-host-ops/responses": (0o2750, "requests"),
    "/var/spool/paperclip-host-ops/.staging": (0o700, "reader"),
    "/var/spool/paperclip-host-ops/processing": (0o700, "reader"),
    "/var/spool/paperclip-host-ops/quarantine": (0o700, "reader"),
}
MODES = {name: 0o644 for name in state.FILES}
MODES.update({"broker.py": 0o755, "sudoers": 0o440, "sudoers-mirror": 0o640, "config": 0o640})
READER_GROUP_FILES = {"sudoers-mirror", "config"}


def validate_layout(journal, sources, reader_uid, reader_gid, requests_gid):
    controls = {
        "/usr/local/libexec/paperclip-host-ops": (0o755, journal.owner, os.getgid()),
        "/usr/local/share/doc/paperclip-host-ops": (0o755, journal.owner, os.getgid()),
        "/etc/paperclip-host-ops": (0o750, journal.owner, reader_gid),
    }
    for absolute, (mode, uid, gid) in controls.items():
        path = journal.root / absolute.lstrip("/")
        journal.parents(path)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid) != (mode, uid, gid):
            raise state.Refusal("upgrade requires pre-provisioned control directories")
    for absolute in state.FILES.values():
        path = journal.target(absolute)
        journal.parents(path)
        if os.path.lexists(path):
            _, _, attrs = state.regular(path)
            if attrs:
                raise state.Refusal("managed file attributes require separate upgrade review")
    for absolute in RUNTIME_DIRS:
        current = journal.root
        for part in Path(absolute).parts[1:]:
            current /= part
            info = current.lstat()
            expected = RUNTIME_DIRS.get("/" + str(current.relative_to(journal.root)))
            if expected:
                expected_mode, expected_group = expected
                gid = reader_gid if expected_group == "reader" else requests_gid
                valid = (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid) == (expected_mode, reader_uid, gid)
            else:
                valid = info.st_uid == journal.owner and not info.st_mode & 0o022
            if not stat.S_ISDIR(info.st_mode) or not valid:
                raise state.Refusal("upgrade requires unchanged pre-provisioned runtime directories")
    rows = {}
    template, _, _ = state.regular(sources["host-ops-broker.tmpfiles"])
    for line in template.decode().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 6 or fields[0] != "d" or fields[-1] != "-" or fields[1] in rows:
            raise state.Refusal("unsupported tmpfiles upgrade")
        rows[fields[1]] = (int(fields[2], 8), fields[3], fields[4])
    expected = {path: (mode, "paperclip-host-reader", "paperclip-host-" + group)
                for path, (mode, group) in RUNTIME_DIRS.items()}
    if rows != expected:
        raise state.Refusal("directory-changing tmpfiles upgrade requires separate preparation")
    config, _, _ = state.regular(sources["config"])
    config = json.loads(config)
    if not isinstance(config, dict) or config.get("writesEnabled") is not False or config.get("testMode") is not False:
        raise state.Refusal("transaction requires explicitly disabled production config")
    validate_company_provisioning(journal, config, reader_uid, reader_gid, requests_gid)


def validate_company_provisioning(journal, config, reader_uid, reader_gid, requests_gid):
    """Refuse an upgrade whose company-spool provisioning disagrees with config.

    The broker only reads the allow-list it is given; it cannot tell a missing
    or widened company tree from an unconfigured company, so the installer must
    hold the provisioning side before any mutation. With no configured company
    this is a no-op (flat spool only). With entries, every configured company
    must already exist on the live tree with the exact layout-contract
    mode/ownership — including the 0700 broker-owned companies/ guard, whose
    traverse bit the broker cannot see — or the upgrade refuses before the
    service is touched. The installer never creates company directories: a
    missing view is a provisioning failure, not a shortcut to take here.
    """
    spool = config.get("spoolPath")
    entries = config.get("companySpools", [])
    try:
        validated = provisioning.validate_company_spools(entries, spool)
    except (provisioning.Refusal, TypeError, AttributeError):
        raise state.Refusal("company-spool configuration is not provisionable") from None
    if not validated:
        return
    try:
        provisioning.validate_provisioned_tree(
            journal.root, spool, validated, reader_uid, reader_gid, requests_gid)
    except provisioning.Refusal as exc:
        raise state.Refusal("company-spool provisioning differs from configuration: "
                            + str(exc)) from None


def publish(journal, record, name, source, reader_gid):
    absolute = state.FILES[name]
    path = journal.target(absolute)
    journal.parents(path)
    journal.check_restore(record)
    data, _, _ = state.regular(source)
    if state.digest(data) != record["files"][absolute]["afterSha256"]:
        raise state.Refusal("staged source changed after snapshot")
    gid = reader_gid if name in READER_GROUP_FILES else os.getgid()
    fd, temporary = tempfile.mkstemp(prefix=".host-ops-publish-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchown(stream.fileno(), journal.owner, gid)
            os.fchmod(stream.fileno(), MODES[name])
            # Do not silently retain inherited ACLs or transplant file capabilities.
            # Attribute-bearing deployments need an explicit reviewed policy first.
            if os.listxattr(stream.fileno()):
                raise state.Refusal("publication inherited unsupported file attributes")
            # Source: https://docs.python.org/3/library/os.html#os.fsync
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        journal.sync(path.parent)
        observed, info, _ = state.regular(path)
        if state.digest(observed) != state.digest(data) or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (journal.owner, gid, MODES[name]):
            raise state.Refusal("published file verification failed")
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


@contextmanager
def interrupted_install():
    previous = {}
    def interrupt(signum, frame):
        raise state.Refusal("installation interrupted; attempting rollback")
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, interrupt)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def verify_installed_metadata(journal, record, reader_gid):
    journal.check_restore(record)
    for name, absolute in state.FILES.items():
        _, info, attrs = state.regular(journal.target(absolute))
        gid = reader_gid if name in READER_GROUP_FILES else os.getgid()
        if attrs or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (journal.owner, gid, MODES[name]):
            raise state.Refusal("installed file metadata differs from publication contract")
    for absolute, saved in record["keyMetadata"].items():
        info = journal.target(absolute).lstat()
        state.check_key_identity(info, saved)
        if any(state.metadata(info)[key] != saved[key] for key in ("mode", "uid", "gid")):
            raise state.Refusal("signing-key ownership or mode changed during installation")


def apply(journal, sources, source_ref, reader_uid, reader_gid, requests_gid):
    # Library entrypoint retained for offline fixtures. The production CLI uses
    # prepare_journal + apply_prepared so partial setup never becomes an install.
    validate_layout(journal, sources, reader_uid, reader_gid, requests_gid)
    journal.snapshot(sources, source_ref, state.service_state())
    apply_prepared(journal, sources, source_ref, reader_uid, reader_gid, requests_gid)


def apply_prepared(journal, sources, source_ref, reader_uid, reader_gid, requests_gid):
    if not re.fullmatch(r"install-[A-Za-z0-9]+", journal.directory.name):
        raise state.Refusal("only a published install journal may change live state")
    record = journal.load()
    if record["phase"] != "prepared" or record["sourceRef"] != source_ref or set(sources) != set(state.FILES):
        raise state.Refusal("prepared journal differs from the requested upgrade")
    validate_layout(journal, sources, reader_uid, reader_gid, requests_gid)
    # Promotion creates a gap between capture and stop. Refuse independent edits
    # and source/service drift BEFORE entering the mutation/rollback region.
    journal.verify_files(record)
    for name, absolute in state.FILES.items():
        data, _, _ = state.regular(sources[name])
        if state.digest(data) != record["files"][absolute]["afterSha256"]:
            raise state.Refusal("staged source changed before service mutation")
    if state.service_state() != record["service"]:
        raise state.Refusal("service state changed after preparation")
    with interrupted_install():
        try:
            state.systemctl("stop", state.UNIT)
            if state.service_state(require_stable=False)["active"] != "inactive":
                raise state.Refusal("broker did not stop before publication")
            for name in state.FILES:
                publish(journal, record, name, sources[name], reader_gid)
            # Existing keys and all data directories are untouched. Upgrade only:
            # ownership/mode must already match the broker's deployed contract.
            state.systemctl("daemon-reload")
            state.systemctl("enable", state.UNIT)
            state.systemctl("start", state.UNIT)
            verify_installed_metadata(journal, record, reader_gid)
            state.mark_installed(journal)
        except BaseException:
            # A second TERM/INT must not interrupt the recovery attempt.
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, signal.SIG_IGN)
            try:
                state.rollback(journal)
            except BaseException:
                raise state.Refusal("automatic rollback incomplete; retain journal for operator recovery") from None
            raise state.Refusal("installation failed; prior managed files and service state restored") from None


@contextmanager
def install_lock(root=Path("/"), owner=0):
    directory = root / str(state.JOURNAL_ROOT).lstrip("/")
    probe = state.InstallState(directory, root, owner)
    probe.parents(directory)
    try:
        directory.mkdir(mode=0o700)
        probe.sync(directory.parent)
    except FileExistsError:
        pass
    probe.validate_directory()
    fd = os.open(directory / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != owner or stat.S_IMODE(info.st_mode) != 0o600:
            raise state.Refusal("untrusted installer lock")
        # Cooperative, nonblocking lock; never unlink the lock file.
        # Source: https://docs.python.org/3/library/fcntl.html#fcntl.flock
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise state.Refusal("installer lock unavailable") from None
        yield directory
    finally:
        os.close(fd)


def require_no_unfinished_install(directory, root=Path("/"), owner=0):
    # New prepare-* directories are never mutation-capable; retain them as
    # evidence, without trusting their contents or silently deleting them.
    # Legacy install-* journals lacking state remain ambiguous and fail closed.
    for prior in directory.glob("install-*"):
        if not os.path.lexists(prior / "state.json"):
            raise state.Refusal("incomplete legacy journal setup requires operator inspection before retry")
        if state.InstallState(prior, root, owner).load()["phase"] == "prepared":
            raise state.Refusal("unfinished installation requires rollback before another apply")


def promote_journal(source, destination):
    # Never replace even an empty destination directory. Both paths are siblings
    # in the protected journal root; do not fall back to non-atomic copying.
    # https://man7.org/linux/man-pages/man2/rename.2.html
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None:
        raise state.Refusal("journal publication requires atomic no-replace rename")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    at_fdcwd, rename_noreplace = -100, 1  # Linux UAPI constants; not exposed by Python os.
    if rename(at_fdcwd, os.fsencode(source), at_fdcwd, os.fsencode(destination), rename_noreplace) != 0:
        error = ctypes.get_errno()
        raise state.Refusal("journal publication failed (" + os.strerror(error)
                            + "); retain preparation and any destination journal")


def prepare_journal(directory, source_dir, sources, source_ref, reader_uid, reader_gid,
                    requests_gid, root=Path("/"), owner=0):
    """Under install_lock, build then publish a complete recoverable journal.

    No service changes or live-file publication occur here. Any interrupted
    prepare-* is retained; only install-* is accepted by apply/rollback CLI.
    Root/owner parameters are library-only offline fixture boundaries.
    """
    container = state.InstallState(directory, root, owner)
    container.validate_directory()
    require_no_unfinished_install(directory, root, owner)
    token = secrets.token_hex(12)
    path = directory / ("prepare-" + token)
    journal = state.InstallState(path, root, owner)
    validate_layout(journal, sources, reader_uid, reader_gid, requests_gid)
    # The rollback entrypoint re-executes this module from the journal, so
    # every module it imports at top level must be staged alongside it. A new
    # top-level import without a staged helper would make rollback fail with
    # ModuleNotFoundError after the live files already moved.
    helpers = {name: state.regular(source_dir / name)[0]
               for name in ("install_state.py", "install_transaction.py",
                            "spool_provisioning.py")}
    path.mkdir(mode=0o700)
    journal.sync(directory)
    for name, data in helpers.items():
        with open(path / name, "xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    journal.snapshot(sources, source_ref, state.service_state())
    # Reopen all recovery material before the install-* visibility boundary.
    record = journal.load()
    if record["phase"] != "prepared" or record["sourceRef"] != source_ref:
        raise state.Refusal("prepared recovery record differs")
    for name, expected in helpers.items():
        data, info, attrs = state.regular(path / name)
        if data != expected or info.st_uid != owner or stat.S_IMODE(info.st_mode) != 0o600 or attrs:
            raise state.Refusal("prepared recovery helper differs")
    journal.sync(path)
    destination = directory / ("install-" + token)
    promote_journal(path, destination)
    # If this fsync fails, destination remains prepared and blocks a retry. No
    # service was touched, but durability is unknown; never report success.
    journal.sync(directory)
    published = state.InstallState(destination, root, owner)
    if published.load() != record:
        raise state.Refusal("published recovery record differs")
    return published


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("apply", "rollback"))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--source-ref")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--sudoers", type=Path)
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0 or os.getgid() != 0:
            raise state.Refusal("installer transaction requires root:root")
        with install_lock() as directory:
            if args.command == "rollback":
                if args.state is None or args.state.parent != directory or not re.fullmatch(r"install-[A-Za-z0-9]+", args.state.name):
                    raise state.Refusal("rollback needs a protected install journal")
                state.rollback(state.InstallState(args.state))
                return 0
            if args.state is not None or any(value is None for value in (args.source_dir, args.source_ref, args.config, args.sudoers)):
                raise state.Refusal("apply requires reviewed source arguments, not a supplied journal")
            require_no_unfinished_install(directory)
            reader = pwd.getpwnam("paperclip-host-reader")
            reader_gid = grp.getgrnam("paperclip-host-reader").gr_gid
            requests_gid = grp.getgrnam("paperclip-host-requests").gr_gid
            if not all(0 < value < 1000 for value in (reader.pw_uid, reader_gid, requests_gid)) or reader.pw_gid != reader_gid:
                raise state.Refusal("pre-provisioned broker identity is required")
            sources = {name: args.source_dir / name for name in state.FILES}
            sources.update({"config": args.config, "sudoers": args.sudoers,
                            "sudoers-mirror": args.sudoers, "public-key": Path(state.KEYS[1])})
            probe = state.InstallState(directory)
            for absolute, mode in zip(state.KEYS, (0o600, 0o644)):
                probe.parents(Path(absolute), key_path=True)
                info = Path(absolute).lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (reader.pw_uid, reader_gid, mode):
                    raise state.Refusal("upgrade requires existing broker-owned keys with unchanged modes")
            journal = prepare_journal(directory, args.source_dir, sources, args.source_ref,
                                      reader.pw_uid, reader_gid, requests_gid)
            path = journal.directory
            print("INSTALL JOURNAL: " + str(path), flush=True)
            print("ROLLBACK: /usr/bin/python3 -B " + str(path / "install_transaction.py") + " rollback --state " + str(path), flush=True)
            apply_prepared(journal, sources, args.source_ref, reader.pw_uid, reader_gid, requests_gid)
    except (state.Refusal, OSError, ValueError, TypeError, KeyError) as exc:
        message = str(exc) if isinstance(exc, state.Refusal) else "installer transaction failed; retain any emitted journal"
        print("REFUSED: " + message, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
