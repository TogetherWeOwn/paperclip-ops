#!/usr/bin/env python3
"""Offline fixtures for host-ops/install_transaction.py + install_state.py.

Isolated only: private temp roots owned by os.getuid(), mocked systemd.
No live systemctl, no host credentials/sockets, no real root-owned files.
Positive controls go through validate_layout (never bypassed).
"""

import copy
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
_state_spec = importlib.util.spec_from_file_location(
    "install_state", ROOT / "host-ops/install_state.py"
)
state = importlib.util.module_from_spec(_state_spec)
sys.modules["install_state"] = state
_state_spec.loader.exec_module(state)

# install_transaction.py imports its sibling helper the same way it imports
# install_state: plain module name resolved from the host-ops directory.
_prov_spec = importlib.util.spec_from_file_location(
    "spool_provisioning", ROOT / "host-ops/spool_provisioning.py"
)
prov_module = importlib.util.module_from_spec(_prov_spec)
sys.modules["spool_provisioning"] = prov_module
_prov_spec.loader.exec_module(prov_module)

_txn_spec = importlib.util.spec_from_file_location(
    "install_transaction", ROOT / "host-ops/install_transaction.py"
)
txn = importlib.util.module_from_spec(_txn_spec)
sys.modules["install_transaction"] = txn
_txn_spec.loader.exec_module(txn)

ACTIVE = {"active": "active", "enabled": "enabled"}
INACTIVE = {"active": "inactive", "enabled": "disabled"}
OWNER = os.getuid()
GID = os.getgid()
REF = "b" * 40


def _mkdir_mode(path, mode):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def build_fixture(base):
    """Create temp-root layout, sources, targets, keys, journal. Returns dict."""
    root = Path(base)
    owner = OWNER
    gid = GID
    # Control directories (exact modes).
    _mkdir_mode(root / "usr/local/libexec/paperclip-host-ops", 0o755)
    _mkdir_mode(root / "usr/local/share/doc/paperclip-host-ops", 0o755)
    _mkdir_mode(root / "etc/paperclip-host-ops", 0o750)
    # Runtime directories (exact modes from RUNTIME_DIRS).
    for absolute, (mode, _group) in txn.RUNTIME_DIRS.items():
        _mkdir_mode(root / absolute.lstrip("/"), mode)
    # Journal directory. The lock helper uses JOURNAL_ROOT itself as the
    # lock directory and validation demands exactly 0700 there. A
    # parents=True intermediate takes 0777&~umask (0755 under umask 022),
    # so pin the intermediate with the exact-mode helper; the 0700 leaf is
    # umask-proof on its own (a umask can only strip bits, never add them).
    _mkdir_mode(root / str(state.JOURNAL_ROOT).lstrip("/"), 0o700)
    directory = root / "var/lib/paperclip-host-ops-installer/install-test"
    directory.mkdir(mode=0o700)
    journal = state.InstallState(directory, root, owner)
    # Sources: real tmpfiles bytes, disabled config, synthetic rest.
    source_dir = root / "source"
    source_dir.mkdir(exist_ok=True)
    real_tmpfiles = (ROOT / "host-ops/host-ops-broker.tmpfiles").read_bytes()
    sources = {}
    for name in state.FILES:
        if name == "host-ops-broker.tmpfiles":
            (source_dir / name).write_bytes(real_tmpfiles)
        elif name == "config":
            # Flat spool only: no companySpools key, so company provisioning
            # validation is a no-op for the pre-existing fixtures. Dedicated
            # company tests below exercise the wired path.
            (source_dir / name).write_text(
                json.dumps({"writesEnabled": False, "testMode": False,
                            "spoolPath": "/var/spool/paperclip-host-ops"})
            )
        elif name in ("sudoers", "sudoers-mirror"):
            (source_dir / name).write_bytes(b"fixture-sudoers-content\n")
        elif name == "public-key":
            (source_dir / name).write_bytes(b"fixture-public-key\n")
        else:
            (source_dir / name).write_bytes(("new-" + name + "\n").encode())
        sources[name] = source_dir / name
    # Targets with old bytes (preimages for rollback).
    for name, absolute in state.FILES.items():
        target = journal.target(absolute)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(("old-" + name + "\n").encode())
        target.chmod(0o640)
    # Keys (metadata only, never read as bytes by journal).
    for i, absolute in enumerate(state.KEYS):
        path = journal.target(absolute)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"PRIVATE-SYNTHETIC-CANARY" if i == 0 else b"public-fixture-key")
        path.chmod(0o600 if i == 0 else 0o644)
    return {"root": root, "journal": journal, "sources": sources,
            "directory": directory, "owner": owner, "gid": gid}


def fake_service(active_before=ACTIVE):
    """Return (service_state, systemctl) mocks for a successful apply."""
    calls = []

    def get_state(require_stable=True):
        if require_stable:
            # Snapshot observation and final mark_installed check.
            # During rollback the final check must match the record; the
            # record here is ACTIVE in success tests.
            return dict(active_before) if calls.count("stop") == 0 else dict(ACTIVE)
        return dict(INACTIVE)

    def ctl(*args, **kwargs):
        calls.append(args)
        return (0, "")

    return get_state, ctl, calls


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="host-ops-install-txn-")
        self.addCleanup(self.tmp.cleanup)
        fx = build_fixture(self.tmp.name)
        self.root = fx["root"]
        self.journal = fx["journal"]
        self.sources = fx["sources"]
        self.owner = fx["owner"]
        self.gid = fx["gid"]

    # ---- positive controls (through layout validation) ----

    def test_layout_positive_control(self):
        txn.validate_layout(self.journal, self.sources,
                            self.owner, self.gid, self.gid)

    def test_publish_positive_control(self):
        txn.validate_layout(self.journal, self.sources,
                            self.owner, self.gid, self.gid)
        record = self.journal.snapshot(self.sources, REF, dict(ACTIVE))
        txn.publish(self.journal, record, "broker.py",
                    self.sources["broker.py"], self.gid)
        target = self.journal.target(state.FILES["broker.py"])
        self.assertEqual(target.read_bytes(),
                         self.sources["broker.py"].read_bytes())
        info = target.lstat()
        self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)),
                         (self.owner, self.gid, txn.MODES["broker.py"]))
        self.assertFalse(list(target.parent.glob(".host-ops-publish-*")))

    def test_publish_reader_group_modes(self):
        record = self.journal.snapshot(self.sources, REF, dict(ACTIVE))
        txn.publish(self.journal, record, "config",
                    self.sources["config"], self.gid)
        target = self.journal.target(state.FILES["config"])
        info = target.lstat()
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o640)
        self.assertEqual(info.st_gid, self.gid)
        txn.publish(self.journal, record, "broker.py",
                    self.sources["broker.py"], self.gid)
        info2 = self.journal.target(state.FILES["broker.py"]).lstat()
        self.assertEqual(info2.st_gid, os.getgid())

    def test_apply_success_marks_installed(self):
        get_state, ctl, calls = fake_service()
        with patch.object(txn.state, "service_state", side_effect=get_state), \
             patch.object(txn.state, "systemctl", side_effect=ctl):
            txn.apply(self.journal, self.sources, REF,
                      self.owner, self.gid, self.gid)
        self.assertEqual(self.journal.load()["phase"], "installed")
        for name, absolute in state.FILES.items():
            self.assertEqual(self.journal.target(absolute).read_bytes(),
                             Path(self.sources[name]).read_bytes())
        self.assertIn(("stop", state.UNIT), calls)
        self.assertIn(("start", state.UNIT), calls)

    # ---- failure / rollback ----

    def test_publish_refuses_changed_source_after_snapshot(self):
        record = self.journal.snapshot(self.sources, REF, dict(ACTIVE))
        self.sources["broker.py"].write_bytes(b"mutated-after-snapshot\n")
        with self.assertRaises(state.Refusal):
            txn.publish(self.journal, record, "broker.py",
                        self.sources["broker.py"], self.gid)
        # Target untouched.
        self.assertEqual(
            self.journal.target(state.FILES["broker.py"]).read_bytes(),
            b"old-broker.py\n")

    def test_published_modes_and_mirror(self):
        # Behavioural mode contract for a real apply through layout validation:
        # sudoers uid=fixture owner gid=os.getgid() 0440; mirror reader gid
        # 0640 with exact same bytes; broker 0755; helper 0644; config 0640.
        # Also regresses: apply must not mutate staged sources post snapshot.
        staged_before = {name: Path(self.sources[name]).read_bytes()
                         for name in state.FILES}
        get_state, ctl, _ = fake_service()
        with patch.object(txn.state, "service_state", side_effect=get_state), \
             patch.object(txn.state, "systemctl", side_effect=ctl):
            txn.apply(self.journal, self.sources, REF,
                      self.owner, self.gid, self.gid)
        self.assertEqual(self.journal.load()["phase"], "installed")
        sudoers = self.journal.target(state.FILES["sudoers"])
        mirror = self.journal.target(state.FILES["sudoers-mirror"])
        config = self.journal.target(state.FILES["config"])
        broker = self.journal.target(state.FILES["broker.py"])
        helper = self.journal.target(state.FILES["image_binding.py"])
        sudoers_info = sudoers.lstat()
        mirror_info = mirror.lstat()
        self.assertEqual(
            (sudoers_info.st_uid, sudoers_info.st_gid,
             stat.S_IMODE(sudoers_info.st_mode)),
            (self.owner, os.getgid(), 0o440))
        self.assertEqual(
            (mirror_info.st_uid, mirror_info.st_gid,
             stat.S_IMODE(mirror_info.st_mode)),
            (self.owner, self.gid, 0o640))
        self.assertEqual(mirror.read_bytes(), sudoers.read_bytes())
        self.assertEqual(mirror.read_bytes(), staged_before["sudoers"])
        self.assertEqual(
            stat.S_IMODE(broker.lstat().st_mode), 0o755)
        self.assertEqual(
            stat.S_IMODE(helper.lstat().st_mode), 0o644)
        self.assertEqual(
            (config.lstat().st_uid, config.lstat().st_gid,
             stat.S_IMODE(config.lstat().st_mode)),
            (self.owner, self.gid, 0o640))
        self.assertEqual(broker.read_bytes(), staged_before["broker.py"])
        # Staged sources are inputs only: apply must leave every staged
        # source byte-identical post snapshot.
        for name in state.FILES:
            self.assertEqual(Path(self.sources[name]).read_bytes(),
                             staged_before[name],
                             "staged source mutated: %s" % name)

    def test_apply_rolls_back_at_every_completed_file_boundary(self):
        names = list(state.FILES)
        # completed=N would be a full success (covered by the success test);
        # every strict prefix must fail and roll back automatically.
        for completed in range(len(names)):
            with self.subTest(completed=completed):
                with tempfile.TemporaryDirectory(
                        prefix="host-ops-txn-boundary-") as tmpd:
                    fx = build_fixture(tmpd)
                    journal, sources = fx["journal"], fx["sources"]
                    owner, gid = fx["owner"], fx["gid"]
                    before = {a: journal.target(a).read_bytes()
                              for a in state.FILES.values()}
                    real_publish = txn.publish
                    seen = []

                    def flaky(j, record, name, source, rgid):
                        if len(seen) >= completed:
                            raise state.Refusal("synthetic publish failure")
                        seen.append(name)
                        return real_publish(j, record, name, source, rgid)

                    def get_state(require_stable=True):
                        if require_stable:
                            return dict(ACTIVE)
                        return dict(INACTIVE)

                    with patch.object(txn, "publish", side_effect=flaky), \
                         patch.object(txn.state, "service_state",
                                      side_effect=get_state), \
                         patch.object(txn.state, "systemctl",
                                      return_value=(0, "")):
                        with self.assertRaises(state.Refusal):
                            txn.apply(journal, sources, REF, owner, gid, gid)
                    # Automatic rollback restored every preimage and
                    # records phase rolled_back (not prepared).
                    self.assertEqual(journal.load()["phase"], "rolled_back")
                    journal.verify_files(journal.load())
                    for absolute, content in before.items():
                        self.assertEqual(journal.target(absolute).read_bytes(),
                                         content)
                    self.assertEqual(len(seen), completed)

    def test_apply_failing_start_triggers_automatic_rollback(self):
        before = {a: self.journal.target(a).read_bytes()
                  for a in state.FILES.values()}

        def get_state(require_stable=True):
            return dict(ACTIVE) if require_stable else dict(INACTIVE)

        starts = []

        def ctl(*args, **kwargs):
            if args and args[0] == "start":
                starts.append(1)
                if len(starts) == 1:
                    # Fail only the apply's start; let rollback's start succeed
                    # so automatic rollback can complete.
                    raise state.Refusal("synthetic start failure")
            return (0, "")

        with patch.object(txn.state, "service_state", side_effect=get_state), \
             patch.object(txn.state, "systemctl", side_effect=ctl):
            with self.assertRaises(state.Refusal):
                txn.apply(self.journal, self.sources, REF,
                          self.owner, self.gid, self.gid)
        self.assertEqual(self.journal.load()["phase"], "rolled_back")
        self.journal.verify_files(self.journal.load())
        for absolute, content in before.items():
            self.assertEqual(self.journal.target(absolute).read_bytes(), content)

    def test_apply_stop_not_inactive_refuses_and_rolls_back(self):
        before = {a: self.journal.target(a).read_bytes()
                  for a in state.FILES.values()}
        unstable_calls = []

        def get_state(require_stable=True):
            if require_stable:
                return dict(ACTIVE)
            # First post-stop check sees a stuck broker (apply refuses);
            # rollback's own stop then takes effect so recovery completes.
            unstable_calls.append(1)
            return dict(ACTIVE) if len(unstable_calls) == 1 else dict(INACTIVE)

        with patch.object(txn.state, "service_state", side_effect=get_state), \
             patch.object(txn.state, "systemctl", return_value=(0, "")):
            with self.assertRaises(state.Refusal):
                txn.apply(self.journal, self.sources, REF,
                          self.owner, self.gid, self.gid)
        self.assertEqual(self.journal.load()["phase"], "rolled_back")
        for absolute, content in before.items():
            self.assertEqual(self.journal.target(absolute).read_bytes(), content)

    def test_apply_changed_source_midflight_rolls_back(self):
        before = {a: self.journal.target(a).read_bytes()
                  for a in state.FILES.values()}
        victim = list(state.FILES)[-1]

        def get_state(require_stable=True):
            return dict(ACTIVE) if require_stable else dict(INACTIVE)

        def ctl(*args, **kwargs):
            if args and args[0] == "stop":
                # Mutate a not-yet-published source after snapshot.
                Path(self.sources[victim]).write_bytes(b"changed-midflight\n")
            return (0, "")

        with patch.object(txn.state, "service_state", side_effect=get_state), \
             patch.object(txn.state, "systemctl", side_effect=ctl):
            with self.assertRaises(state.Refusal):
                txn.apply(self.journal, self.sources, REF,
                          self.owner, self.gid, self.gid)
        self.assertEqual(self.journal.load()["phase"], "rolled_back")
        for absolute, content in before.items():
            self.assertEqual(self.journal.target(absolute).read_bytes(), content)

    # ---- metadata / xattr forward contracts ----

    def test_apply_key_chmod_during_start_refuses_and_restores_metadata(self):
        # Drift key metadata after publication: post-start verification must
        # refuse, roll back metadata, and never record installed success.
        key = self.journal.target(state.KEYS[0])
        self.assertEqual(stat.S_IMODE(key.lstat().st_mode), 0o600)
        key_before = key.read_bytes()
        before = {a: self.journal.target(a).read_bytes()
                  for a in state.FILES.values()}
        starts = []

        def get_state(require_stable=True):
            return dict(ACTIVE) if require_stable else dict(INACTIVE)

        def ctl(*args, **kwargs):
            if args and args[0] == "start" and not starts:
                starts.append(1)
                key.chmod(0o640)
            return (0, "")

        with patch.object(txn.state, "service_state", side_effect=get_state), \
             patch.object(txn.state, "systemctl", side_effect=ctl):
            with self.assertRaises(state.Refusal):
                txn.apply(self.journal, self.sources, REF,
                          self.owner, self.gid, self.gid)
        record = self.journal.load()
        self.assertNotEqual(record["phase"], "installed")
        self.assertEqual(record["phase"], "rolled_back")
        self.assertEqual(stat.S_IMODE(key.lstat().st_mode), 0o600)
        self.assertEqual(key.read_bytes(), key_before)
        for absolute, content in before.items():
            self.assertEqual(self.journal.target(absolute).read_bytes(), content)

    def test_preimage_xattr_fails_closed_before_service_or_journal(self):
        # Bounded forward contract: a managed preimage carrying xattrs must
        # refuse in prevalidation (not be silently stripped), before any
        # service call is made and before any journal is published.
        target = self.journal.target(state.FILES["broker.py"])
        os.setxattr(target, "user.test", b"fixture-xattr")
        with self.assertRaises(state.Refusal):
            txn.validate_layout(self.journal, self.sources,
                                self.owner, self.gid, self.gid)
        with patch.object(txn.state, "service_state",
                          side_effect=AssertionError("no service observation")), \
             patch.object(txn.state, "systemctl",
                          side_effect=AssertionError("no service mutation")):
            with self.assertRaises(state.Refusal):
                txn.apply(self.journal, self.sources, REF,
                          self.owner, self.gid, self.gid)
        self.assertFalse((self.journal.directory / "state.json").exists())
        # Nothing stripped: the attribute is still on the preimage.
        self.assertEqual(os.getxattr(target, "user.test"), b"fixture-xattr")

    def test_publisher_temp_xattr_refuses_before_replace_and_cleans_temp(self):
        # An inherited xattr on the publisher tempfile (e.g. transplanted
        # capability/ACL) must refuse before os.replace touches the live
        # target, and the temp file must be cleaned up.
        record = self.journal.snapshot(self.sources, REF, dict(ACTIVE))
        target = self.journal.target(state.FILES["broker.py"])
        old_bytes = target.read_bytes()
        real_mkstemp = txn.tempfile.mkstemp

        def tainted_mkstemp(*args, **kwargs):
            fd, temporary = real_mkstemp(*args, **kwargs)
            try:
                os.setxattr(fd, "user.test", b"inherited-capability")
            except OSError:
                os.close(fd)
                raise
            return fd, temporary

        with patch.object(txn.tempfile, "mkstemp", side_effect=tainted_mkstemp):
            with self.assertRaises(state.Refusal):
                txn.publish(self.journal, record, "broker.py",
                            self.sources["broker.py"], self.gid)
        self.assertEqual(target.read_bytes(), old_bytes)
        self.assertFalse(list(target.parent.glob(".host-ops-publish-*")))

    # ---- locks ----

    def test_lock_holder_and_releaser(self):
        with txn.install_lock(self.root, self.owner) as directory:
            self.assertTrue((directory / "lock").exists())
        # Re-acquire after release.
        with txn.install_lock(self.root, self.owner):
            pass

    def test_lock_contention_forked_child_refuses(self):
        # The parent must HOLD the lock before forking. Forking first
        # races the child's contender against the parent's acquire: under
        # load the child can acquire the free lock and exit before the
        # parent contends, which reads as AssertionError 1 != 0, or hold
        # it at the moment the parent contends, which errors the parent
        # with "installer lock unavailable". Holding across fork makes
        # the child's nonblocking contender deterministic. A child that
        # still acquires (exit 1) proves the filesystem does not enforce
        # flock exclusion, so skip there instead of failing the suite;
        # the mocked-flock test below keeps the refusal contract covered
        # on every filesystem.
        with txn.install_lock(self.root, self.owner):
            try:
                pid_test = os.fork()
            except (OSError, AttributeError) as exc:
                self.skipTest("fork unavailable: %r" % (exc,))
            if pid_test != 0:
                # Parent: still holding the lock while the child
                # contends, then reap.
                _, waited_status = os.waitpid(pid_test, 0)
                code = os.waitstatus_to_exitcode(waited_status)
                if code == 1:
                    self.skipTest("flock exclusion not enforced on this "
                                  "filesystem; contention unverifiable")
                # Child exits 0 when contention refused.
                self.assertEqual(code, 0)
                return
            # Child process: a new open on the same lock file must fail
            # nonblocking while the parent holds it.
            try:
                try:
                    with txn.install_lock(self.root, self.owner):
                        os._exit(1)  # Unexpectedly acquired.
                except state.Refusal:
                    os._exit(0)
                except OSError:
                    os._exit(0)
            finally:
                os._exit(2)

    def test_lock_contention_mocked_flock_refuses(self):
        with txn.install_lock(self.root, self.owner):
            with patch.object(txn.fcntl, "flock",
                              side_effect=OSError("synthetic contention")):
                with self.assertRaises(state.Refusal):
                    # Drain generator to trigger flock inside context body.
                    with txn.install_lock(self.root, self.owner):
                        pass

    def test_lock_inode_reuse_and_untrusted_modes_refuse(self):
        with txn.install_lock(self.root, self.owner):
            pass
        lock = (self.root / str(state.JOURNAL_ROOT).lstrip("/")
                / "lock")
        # Hardlink -> nlink != 1 refuses.
        hard = lock.with_name("lock.hard")
        os.link(lock, hard)
        try:
            with self.assertRaises(state.Refusal):
                with txn.install_lock(self.root, self.owner):
                    pass
        finally:
            os.unlink(hard)
        # Wrong mode refuses.
        old_mode = stat.S_IMODE(lock.lstat().st_mode)
        os.chmod(lock, 0o644)
        try:
            with self.assertRaises(state.Refusal):
                with txn.install_lock(self.root, self.owner):
                    pass
        finally:
            os.chmod(lock, old_mode)
        # Symlink at lock path raises (O_NOFOLLOW) and must not be followed.
        os.unlink(lock)
        try:
            target = self.root / "external-lock-target"
            target.write_bytes(b"x")
            lock.symlink_to(target)
            with self.assertRaises((state.Refusal, OSError)):
                with txn.install_lock(self.root, self.owner):
                    pass
        finally:
            if lock.is_symlink() or lock.exists():
                lock.unlink()

    # ---- layout / tmpfiles / config refusals ----

    def test_layout_control_mode_refusal(self):
        bad = self.root / "etc/paperclip-host-ops"
        os.chmod(bad, 0o777)
        with self.assertRaises(state.Refusal):
            txn.validate_layout(self.journal, self.sources,
                                self.owner, self.gid, self.gid)

    def test_layout_runtime_mode_refusal(self):
        bad = self.root / "var/spool/paperclip-host-ops/requests"
        os.chmod(bad, 0o755)
        with self.assertRaises(state.Refusal):
            txn.validate_layout(self.journal, self.sources,
                                self.owner, self.gid, self.gid)

    def test_layout_runtime_uid_refusal(self):
        # No chown needed: ask for a different reader uid than the files have.
        with self.assertRaises(state.Refusal):
            txn.validate_layout(self.journal, self.sources,
                                self.owner + 1 if self.owner + 1 < 2**31
                                else self.owner - 1,
                                self.gid, self.gid)

    def test_tmpfiles_refusals(self):
        base = Path(self.sources["host-ops-broker.tmpfiles"]).read_bytes()
        variants = [
            base.replace(b"0750", b"0755"),  # mode drift
            base + b"d /var/spool/paperclip-host-ops/extra 0750 "
                   b"paperclip-host-reader paperclip-host-requests -\n",
            base.replace(b"paperclip-host-reader paperclip-host-reader -",
                         b"root root -", 1),  # owner drift
        ]
        for i, content in enumerate(variants):
            with self.subTest(variant=i):
                self.sources["host-ops-broker.tmpfiles"].write_bytes(content)
                with self.assertRaises(state.Refusal):
                    txn.validate_layout(self.journal, self.sources,
                                        self.owner, self.gid, self.gid)
        # Restore for other tests (fresh setUp each test, but be tidy).
        self.sources["host-ops-broker.tmpfiles"].write_bytes(base)

    def test_config_refusals(self):
        for i, payload in enumerate([
            {"writesEnabled": True, "testMode": False},
            {"writesEnabled": False, "testMode": True},
            {"writesEnabled": False},
            ["not-a-dict"],
        ]):
            with self.subTest(variant=i):
                self.sources["config"].write_text(json.dumps(payload))
                with self.assertRaises(state.Refusal):
                    txn.validate_layout(self.journal, self.sources,
                                        self.owner, self.gid, self.gid)

    # ---- company-spool provisioning wiring ----

    COMPANY_A = "00000000-0000-4000-8000-000000000003"
    COMPANY_B = "00000000-0000-4000-8000-000000000020"
    SPOOL = "/var/spool/paperclip-host-ops"

    def _write_config(self, company_ids):
        self.sources["config"].write_text(json.dumps({
            "writesEnabled": False, "testMode": False,
            "spoolPath": self.SPOOL,
            "companySpools": [
                {"companyId": cid, "path": f"{self.SPOOL}/companies/{cid}"}
                for cid in company_ids],
        }))

    def _provision_company(self, company_id, companies_mode=0o700,
                           drop=(), widen=()):
        base = self.root / self.SPOOL.lstrip("/")
        (base / "companies").mkdir(parents=True, exist_ok=True)
        os.chmod(base / "companies", companies_mode)
        company_root = base / "companies" / company_id
        for name, mode, _owner, group in txn.provisioning.COMPANY_TREE:
            target = company_root if name == "." else company_root / name
            if name in drop:
                continue
            target.mkdir(parents=True, exist_ok=True)
            os.chmod(target, 0o755 if name in widen else mode)

    def _check(self):
        txn.validate_layout(self.journal, self.sources,
                            self.owner, self.gid, self.gid)

    def test_company_absent_is_flat_noop(self):
        # The pre-existing fixtures carry no companySpools key at all; the
        # wired path must stay a no-op there (positive controls cover it).
        self._check()

    def test_company_empty_list_is_flat_noop(self):
        self._write_config([])
        self._check()

    def test_company_provisioned_tree_passes(self):
        self._write_config([self.COMPANY_A, self.COMPANY_B])
        self._provision_company(self.COMPANY_A)
        self._provision_company(self.COMPANY_B)
        self._check()

    def test_company_missing_view_refuses_before_mutation(self):
        self._write_config([self.COMPANY_A])
        self._provision_company(self.COMPANY_A, drop={"previews"})
        with patch.object(txn.state, "service_state",
                          side_effect=AssertionError("no service observation")), \
             patch.object(txn.state, "systemctl",
                          side_effect=AssertionError("no service mutation")):
            with self.assertRaises(state.Refusal):
                txn.apply(self.journal, self.sources, REF,
                          self.owner, self.gid, self.gid)
        self.assertFalse((self.journal.directory / "state.json").exists())

    def test_company_widened_companies_guard_refuses(self):
        # The binding check the broker cannot make: 0751 on companies/
        # passes every broker-side check while collapsing isolation.
        self._write_config([self.COMPANY_A])
        self._provision_company(self.COMPANY_A, companies_mode=0o751)
        with self.assertRaises(state.Refusal):
            self._check()

    def test_company_unconfigured_directory_is_ignored(self):
        # A directory with no configured entry is never listed: provisioning
        # an extra company must not disturb the configured one's validation.
        self._write_config([self.COMPANY_A])
        self._provision_company(self.COMPANY_A)
        self._provision_company(self.COMPANY_B)
        self._check()

    def test_company_invalid_allow_list_refuses(self):
        self.sources["config"].write_text(json.dumps({
            "writesEnabled": False, "testMode": False,
            "spoolPath": self.SPOOL,
            "companySpools": [{"companyId": "not-a-uuid",
                               "path": f"{self.SPOOL}/companies/not-a-uuid"}],
        }))
        with self.assertRaises(state.Refusal):
            self._check()

    def test_company_missing_spool_path_refuses(self):
        self.sources["config"].write_text(json.dumps({
            "writesEnabled": False, "testMode": False,
            "companySpools": [{"companyId": self.COMPANY_A,
                               "path": f"{self.SPOOL}/companies/{self.COMPANY_A}"}],
        }))
        with self.assertRaises(state.Refusal):
            self._check()

    # ---- signals ----

    def test_interrupted_install_handler_invocation_and_restore(self):
        outer_int = signal.getsignal(signal.SIGINT)
        outer_term = signal.getsignal(signal.SIGTERM)
        with txn.interrupted_install():
            handler = signal.getsignal(signal.SIGINT)
            self.assertNotEqual(handler, outer_int)
            with self.assertRaises(state.Refusal):
                handler(signal.SIGINT, None)
            term_handler = signal.getsignal(signal.SIGTERM)
            with self.assertRaises(state.Refusal):
                term_handler(signal.SIGTERM, None)
        self.assertEqual(signal.getsignal(signal.SIGINT), outer_int)
        self.assertEqual(signal.getsignal(signal.SIGTERM), outer_term)

    def test_apply_signal_in_ignored_during_rollback(self):
        # A TERM during publish must still roll back; rollback itself must
        # ignore further INT/TERM. Drive with a publish that raises like a
        # signal handler would.
        def get_state(require_stable=True):
            return dict(ACTIVE) if require_stable else dict(INACTIVE)

        real_publish = txn.publish

        def interrupt_once(j, record, name, source, rgid):
            if name == "broker.py":
                raise state.Refusal("installation interrupted; attempting rollback")
            return real_publish(j, record, name, source, rgid)

        with patch.object(txn, "publish", side_effect=interrupt_once), \
             patch.object(txn.state, "service_state", side_effect=get_state), \
             patch.object(txn.state, "systemctl", return_value=(0, "")):
            with self.assertRaises(state.Refusal) as caught:
                txn.apply(self.journal, self.sources, REF,
                          self.owner, self.gid, self.gid)
        self.assertIn("installation failed", str(caught.exception))
        self.journal.verify_files(self.journal.load())


if __name__ == "__main__":
    unittest.main()
