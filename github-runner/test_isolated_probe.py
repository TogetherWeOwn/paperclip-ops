"""Mocked unit tests for isolated-probe.py (case_4c57f7c0).

Fully offline: downgraded connect/access/signal, systemctl, /proc, NSS,
socket, and smoke paths are mocked. One test asserts the REAL
subprocess invocation shape (input=, never stdin=). Run from repo root:
python3 github-runner/test_isolated_probe.py
"""
import contextlib
import errno
import hashlib
import importlib.util
import io
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

_HERE = pathlib.Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "isolated_probe", str(_HERE / "isolated-probe.py"))
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)

ISO = "gha-isolated-6"
DOCK = "gha-runner"
SVC = "actions.runner.TogetherWeOwn.coolify-vps-6.service"
POOL = ["actions.runner.TogetherWeOwn.coolify-vps-%d.service" % i
        for i in (1, 2, 3, 4, 5)]
UNIT_SHA = hashlib.sha256(b"fixture-live-unit").hexdigest()
BASE = (["--service-name", SVC, "--isolated-user", ISO,
         "--unit-sha256", UNIT_SHA] +
        sum((["--pool-service", p] for p in POOL), []))
TREE = "/opt/two-isolated/coolify-vps-6"
HOME = "/var/lib/gha-isolated-6"
EXEC_SHAPE = ("{ path=/opt/two-isolated/coolify-vps-6/runsvc.sh ; "
              "argv[]=/opt/two-isolated/coolify-vps-6/runsvc.sh ; }")


class FakePwd:
    def __init__(self, name, uid, gid):
        self.pw_name = name
        self.pw_uid = uid
        self.pw_gid = gid


def healthy_props():
    return {
        "LoadState": "loaded", "ActiveState": "active", "MainPID": "1234",
        "User": ISO, "Group": ISO,
        "NoNewPrivileges": "yes", "CapabilityBoundingSet": "",
        "AmbientCapabilities": "", "ProtectSystem": "strict",
        "ProtectHome": "yes", "PrivateTmp": "yes",
        "ProtectProc": "invisible", "RestrictSUIDSGID": "yes",
        "RestrictNamespaces": "yes", "LockPersonality": "yes",
        "RestrictRealtime": "yes",
        "UMask": "0077", "SupplementaryGroups": "",
        "ExecStart": EXEC_SHAPE,
        "FragmentPath": "/etc/systemd/system/" + SVC,
        "DropInPaths": "",
        "EnvironmentFiles": "",
        "InaccessiblePaths": "/run/docker.sock /var/run/docker.sock /run/containerd",
        "WorkingDirectory": TREE,
        "ReadWritePaths": TREE + " " + HOME,
        "Environment": "HOME=" + HOME,
    }


def healthy_status():
    return {"Uid": "1002 1002 1002 1002", "Gid": "1002 1002 1002 1002",
            "Groups": "1002", "NoNewPrivs": "1",
            "CapEff": "0000000000000000", "CapBnd": "0000000000000000",
            "CapAmb": "0000000000000000"}


def pool_dir(i):
    return "/opt/actions-runners/TogetherWeOwn/coolify-vps-%d" % i


def pool_props(i):
    d = pool_dir(i)
    return {"LoadState": "loaded", "ActiveState": "active", "User": DOCK,
            "MainPID": str(3100 + i),
            "WorkingDirectory": d,
            "ExecStart": ("{ path=%s/runsvc.sh ; argv[]=%s/runsvc.sh ; }" % (d, d))}


class _FakeStat:
    def __init__(self, uid, mode):
        self.st_uid = uid
        self.st_mode = mode


class Harness:
    def __init__(self, tc, iso_name=ISO):
        self.p = []
        self.iso_name = iso_name
        self.iso_home = "/var/lib/" + iso_name
        self.props = healthy_props()
        self.pools = {u: pool_props(i + 1) for i, u in enumerate(POOL)}
        self.status = healthy_status()
        self.connect_code = errno.EACCES
        self.access_code = errno.EACCES
        self.access_fn = None
        self.signal_code = errno.EPERM
        self.pids = [2000, 2001] + [3100 + i for i in (1, 2, 3, 4, 5)]
        self.quiet_side = [0, 1]
        self.smoke_rc = 0
        self.tcp_open = False
        self.smoke_calls = 0
        self.docker_home_exists = True
        self.missing_paths = set()
        # Finding 3: journaled unit digest the live unit must match.
        self.unit_digest = UNIT_SHA
        # Finding 4: enumerated listeners (None = unreadable table).
        self.tcp_listeners = []
        self.unix_sockets = []
        self.tcp_docker_rc = 0
        self.tcp_denied = errno.EACCES
        self.connect_fn = None
        # Finding 6: owner reported for executable-ancestor stats.
        self.stat_uid = 0
        self.stat_overrides = {}
        self._real_stat = os.stat
        if iso_name != ISO:
            self.props["User"] = iso_name
            self.props["Group"] = iso_name
            self.props["Environment"] = "HOME=" + self.iso_home
            self.props["ReadWritePaths"] = TREE + " " + self.iso_home

    def start(self):
        iso = FakePwd(self.iso_name, 1002, 1002)
        dock = FakePwd(DOCK, 1001, 1001)
        self.p = [
            mock.patch.object(probe.os, "geteuid", return_value=0),
            mock.patch("pwd.getpwnam",
                       side_effect=lambda n: iso if n == self.iso_name
                       else dock),
            mock.patch("pwd.getpwall", return_value=[iso, dock]),
            mock.patch.object(probe.os, "getgrouplist", return_value=[1002]),
            mock.patch.object(probe, "systemctl_show",
                              side_effect=self._show),
            mock.patch.object(probe, "read_proc_status",
                              side_effect=lambda pid: dict(self.status) if pid == 1234
                              else {'Uid': '1001 1001 1001 1001'}),
            mock.patch.object(probe, "list_pids_of_uid",
                              side_effect=lambda uid: list(self.pids)),
            mock.patch.object(probe, "connect_errno_as_uid",
                              side_effect=self._connect),
            mock.patch.object(probe, "connect_tcp_errno_as_uid",
                              side_effect=self._tcp_as_uid),
            mock.patch.object(probe, "docker_socket_inodes", return_value={'123'}),
            mock.patch.object(probe, "listening_tcp_endpoints",
                              side_effect=lambda inodes: self.tcp_listeners),
            mock.patch.object(probe, "listening_unix_sockets",
                              side_effect=lambda inodes: self.unix_sockets),
            mock.patch.object(probe.os.path, 'realpath', side_effect=lambda p, **k: p),
            mock.patch.object(probe, "read_unit_bytes",
                              side_effect=lambda *a: self.unit_digest),
            mock.patch.object(probe.os, "stat", side_effect=self._stat),
            mock.patch.object(probe, "access_as_uid",
                              side_effect=self._access),
            mock.patch.object(probe, "signal0_errno_as_uid",
                              side_effect=lambda *a: self.signal_code),
            mock.patch.object(probe, "run_quiet",
                              side_effect=lambda *a, **k: self.quiet_side.pop(0)
                              if self.quiet_side else 1),
            mock.patch.object(probe, "run_smoke_under_service",
                              side_effect=self._smoke),
            mock.patch("os.path.exists", side_effect=self._exists),
            mock.patch("os.path.isdir", side_effect=self._isdir),
            mock.patch("socket.create_connection",
                       side_effect=self._tcp),
        ]
        for m in self.p:
            m.start()
        probe.failures[:] = []
        return self

    def stop(self):
        for m in reversed(self.p):
            m.stop()
        probe.failures[:] = []

    def _show(self, service, props):
        if service == SVC:
            return dict(self.props)
        return dict(self.pools[service])

    def _access(self, path, mode, uid, gid):
        if self.access_fn is not None:
            return self.access_fn(path, mode, uid, gid)
        return self.access_code

    def _isdir(self, path):
        if path in self.missing_paths:
            return False
        if path == probe.DEFAULT_DOCKER_HOME:
            return self.docker_home_exists
        return True

    def _exists(self, path):
        # Healthy host layout: docker pool socket present, rootful absent
        # (reported "not exposed"), no socket for the isolated uid.
        if path in self.missing_paths:
            return False
        if path == "/run/user/1001/docker.sock":
            return True
        if path == "/run/user/1002/docker.sock":
            return False
        if path in ("/run/docker.sock", "/var/run/docker.sock"):
            return False
        return True

    def _smoke(self, *a):
        self.smoke_calls += 1
        return self.smoke_rc

    def _tcp(self, *a, **k):
        if self.tcp_open:
            m = mock.Mock()
            m.close.return_value = None
            return m
        raise OSError(errno.ECONNREFUSED, "refused")

    def _connect(self, path, uid, gid):
        # Explicit daemon-endpoint checks: healthy host denies the isolated UID.
        if self.connect_fn is not None:
            return self.connect_fn(path, uid, gid)
        return 0 if uid == 1001 else self.connect_code

    def _tcp_as_uid(self, host, port, uid, gid):
        # Healthy: docker UID (1001) reaches, isolated UID (1002) denied.
        if uid == 1001:
            return self.tcp_docker_rc
        return self.tcp_denied

    def _stat(self, path, *a, **k):
        # Executable-ancestor chain walks pool runsvc.sh ancestry; report a
        # root-owned, non-group/world-writable boundary by default so the
        # healthy fixture reaches the trust boundary.
        s = str(path)
        if s in self.stat_overrides:
            override = self.stat_overrides[s]
            if isinstance(override, BaseException):
                raise override
            uid, mode = override
            return _FakeStat(uid, mode)
        if "runsvc.sh" in s or s.startswith("/opt/actions-runners") or s in ("/", "/opt"):
            return _FakeStat(self.stat_uid, 0o755)
        return self._real_stat(path, *a, **k)


def run_quiet_main(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        return probe.main(list(args))


class ParseTests(unittest.TestCase):
    def test_requires_five_pool_services(self):
        with self.assertRaises(SystemExit) as c:
            probe.parse_args(["--service-name", SVC, "--isolated-user", ISO,
                              "--pool-service", POOL[0]])
        self.assertEqual(c.exception.code, 2)

    def test_rejects_bad_service_name(self):
        with self.assertRaises(SystemExit):
            probe.parse_args(["--service-name", "not-a-unit",
                              "--isolated-user", ISO] +
                             sum((["--pool-service", p] for p in POOL), []))

    def test_refuses_no_native_pg(self):
        with self.assertRaises(SystemExit) as c:
            probe.parse_args(BASE + ["--no-native-pg"])
        self.assertEqual(c.exception.code, 2)

    def test_refuses_skip_flags(self):
        for flag in ("--skip", "--dry-run"):
            with self.assertRaises(SystemExit):
                probe.parse_args(BASE + [flag])

    def test_parse_exec_shape_real(self):
        self.assertEqual(
            probe.parse_exec_path(EXEC_SHAPE),
            "/opt/two-isolated/coolify-vps-6/runsvc.sh")
        self.assertEqual(probe.parse_exec_path("garbage"), "")
        self.assertEqual(probe.parse_exec_path(""), "")

    def test_expected_paths_exact(self):
        tree, home, runner = probe.expected_paths(SVC, ISO)
        self.assertEqual(tree, TREE)
        self.assertEqual(home, HOME)
        self.assertEqual(runner, "coolify-vps-6")


class ProbeTests(unittest.TestCase):
    def test_full_pass_healthy(self):
        h = Harness(self).start()
        try:
            self.assertEqual(run_quiet_main(BASE), 0)
            self.assertEqual(h.smoke_calls, 1)
        finally:
            h.stop()

    # Finding 3: acceptance binds the exact journaled unit bytes plus the
    # effective fields a drop-in/override can change.
    def test_unit_digest_mismatch_fails(self):
        h = Harness(self).start()
        h.unit_digest = "0" * 64
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_missing_unit_digest_refused(self):
        args = [a for a in BASE if a != "--unit-sha256"]
        sha_index = args.index(UNIT_SHA)
        del args[sha_index]
        with self.assertRaises(SystemExit) as c:
            probe.parse_args(args)
        self.assertEqual(c.exception.code, 2)

    def test_malformed_unit_digest_refused(self):
        args = list(BASE)
        args[args.index(UNIT_SHA)] = "not-hex"
        with self.assertRaises(SystemExit) as c:
            probe.parse_args(args)
        self.assertEqual(c.exception.code, 2)

    def test_dropin_paths_fails(self):
        h = Harness(self).start()
        h.props["DropInPaths"] = "/etc/systemd/system/service.d/override.conf"
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_environment_files_fails(self):
        h = Harness(self).start()
        h.props["EnvironmentFiles"] = "/etc/default/runner"
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_fragment_path_fails(self):
        h = Harness(self).start()
        h.props["FragmentPath"] = "/run/systemd/generator/" + SVC
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_missing_daemon_mask_fails(self):
        h = Harness(self).start()
        h.props["InaccessiblePaths"] = "/run/containerd"
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_wrong_execstart_fails(self):
        h = Harness(self).start()
        h.props["ExecStart"] = ("{ path=/opt/two-isolated/coolify-vps-6/run.sh ; "
                                "argv[]=/opt/two-isolated/coolify-vps-6/run.sh ; }")
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    # Finding 4: enumerated listeners (LAN/Tailscale/nonstandard included)
    # are fingerprinted; an unreachable table fails closed.
    def test_unreadable_listener_tables_fail(self):
        h = Harness(self).start()
        h.tcp_listeners = None
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_nonstandard_tcp_listener_reachable_as_isolated_fails(self):
        h = Harness(self).start()
        h.tcp_listeners = [("192.0.2.39:9999 (/proc/net/tcp)", "192.0.2.39", 9999)]
        h.tcp_denied = 0  # isolated UID connects; must not be blessed
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_container_api_listener_denies_isolated_passes(self):
        h = Harness(self).start()
        h.tcp_listeners = [("192.0.2.39:2376 (/proc/net/tcp)", "192.0.2.39", 2376)]
        h.tcp_denied = errno.EACCES
        try:
            self.assertEqual(run_quiet_main(BASE), 0)
            self.assertEqual(h.smoke_calls, 1)
        finally:
            h.stop()

    def test_container_api_answers_isolated_fails(self):
        h = Harness(self).start()
        h.unix_sockets = ["/run/odd-daemon.sock"]
        h.connect_fn = lambda *a: 0  # isolated UID connects
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_inconclusive_listener_fails(self):
        h = Harness(self).start()
        h.tcp_listeners = [("192.0.2.39:9999 (/proc/net/tcp)", "192.0.2.39", 9999)]
        h.tcp_denied = errno.ETIMEDOUT
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    # Finding 5: all four UID slots enumerate, and every live pool MainPID
    # must be inside the enumerated docker-UID set.
    def test_each_uid_slot_process_enumerated(self):
        for slot in range(4):
            values = ['0'] * 4
            values[slot] = '1001'
            with self.subTest(slot=slot), \
                    mock.patch.object(probe.os, 'listdir', return_value=['4242', 'self']), \
                    mock.patch.object(probe, 'read_proc_status', return_value={'Uid': ' '.join(values)}):
                self.assertEqual(probe.list_pids_of_uid(1001), [4242])

    def test_pool_mainpid_outside_enumerated_set_fails(self):
        h = Harness(self).start()
        h.pids = [2000, 2001]  # live pool MainPIDs 3101..3105 missing
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_pool_without_mainpid_fails(self):
        h = Harness(self).start()
        h.pools[POOL[0]] = dict(h.pools[POOL[0]])
        h.pools[POOL[0]]["MainPID"] = "0"
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    # Finding 6: write denial walks the full executable ancestor chain to
    # a root-owned, non-group/world-writable trust boundary.
    def test_writable_high_ancestor_fails(self):
        h = Harness(self).start()
        h.stat_overrides["/opt"] = (1001, 0o775)  # writable shared ancestor
        h.stat_overrides["/opt/actions-runners"] = (1001, 0o775)
        h.access_fn = lambda path, mode, *a: 0 if path == '/opt' and mode == 'w' else errno.EACCES
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_no_trust_boundary_fails(self):
        h = Harness(self).start()
        h.stat_uid = 1001  # chain reaches no root-owned boundary
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_unreadable_ancestor_fails(self):
        h = Harness(self).start()
        h.stat_overrides["/opt/actions-runners"] = OSError(errno.EACCES,
                                                            "denied")
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_missing_expected_key_fails(self):
        h = Harness(self).start()
        del h.props["AmbientCapabilities"]
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
        finally:
            h.stop()

    def test_partial_uid_mismatch_fails(self):
        h = Harness(self).start()
        h.status = dict(h.status)
        h.status["Uid"] = "1002 1002 0 1002"
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
        finally:
            h.stop()

    def test_prefix_workdir_rejected(self):
        h = Harness(self).start()
        h.props["WorkingDirectory"] = TREE + "-evil"
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
        finally:
            h.stop()

    def test_readwritepaths_superset_rejected(self):
        h = Harness(self).start()
        h.props["ReadWritePaths"] = TREE + " " + HOME + " /tmp"
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
        finally:
            h.stop()

    def test_positive_control_failure_fails(self):
        h = Harness(self).start()
        h.quiet_side = [1, 1]
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
        finally:
            h.stop()

    def test_explicit_connect_success_is_failure(self):
        h = Harness(self).start()
        h.connect_code = 0
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
        finally:
            h.stop()

    def test_tcp_open_is_failure(self):
        h = Harness(self).start()
        h.tcp_open = True
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
        finally:
            h.stop()

    def test_pool_exec_shape_unreadable_fails(self):
        h = Harness(self).start()
        h.pools[POOL[0]] = dict(h.pools[POOL[0]])
        h.pools[POOL[0]]["ExecStart"] = "garbage"
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
        finally:
            h.stop()

    def test_pool_cred_readable_fails(self):
        h = Harness(self).start()
        h.access_code = 0  # everything accessible => pool dir breach
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
        finally:
            h.stop()

    def test_signal_esrch_fails(self):
        h = Harness(self).start()
        h.signal_code = errno.ESRCH
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
        finally:
            h.stop()

    def test_no_docker_pids_fails(self):
        h = Harness(self).start()
        h.pids = []
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
        finally:
            h.stop()

    def test_smoke_failure_fails(self):
        h = Harness(self).start()
        h.smoke_rc = 14
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
        finally:
            h.stop()

    def test_identity_failure_skips_smoke(self):
        h = Harness(self).start()
        h.props["User"] = DOCK
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_mismatched_runner_identity_refused(self):
        other_iso = "gha-isolated-7"
        args = (["--service-name", SVC, "--isolated-user", other_iso] +
                sum((["--pool-service", p] for p in POOL), []))
        with self.assertRaises(SystemExit) as c:
            probe.parse_args(args)
        self.assertEqual(c.exception.code, 2)

    def test_wrong_target_refused(self):
        args = (["--service-name",
                 "actions.runner.TogetherWeOwn.coolify-vps-9.service",
                 "--isolated-user", "gha-isolated-9"] +
                sum((["--pool-service", p] for p in POOL), []))
        with self.assertRaises(SystemExit) as c:
            probe.parse_args(args)
        self.assertEqual(c.exception.code, 2)

    def test_wrong_docker_user_refused(self):
        with self.assertRaises(SystemExit) as c:
            probe.parse_args(BASE + ["--docker-user", "someone-else"])
        self.assertEqual(c.exception.code, 2)

    def test_wrong_pool_set_refused(self):
        other_pool = list(POOL)
        other_pool[0] = ("actions.runner.TogetherWeOwn.coolify-vps-9.service")
        args = (["--service-name", SVC, "--isolated-user", ISO] +
                sum((["--pool-service", p] for p in other_pool), []))
        with self.assertRaises(SystemExit) as c:
            probe.parse_args(args)
        self.assertEqual(c.exception.code, 2)

    def test_missing_docker_home_fails(self):
        h = Harness(self).start()
        h.docker_home_exists = False
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_writable_pool_exec_root_fails(self):
        h = Harness(self).start()
        target = pool_dir(1)

        def access_fn(path, mode, uid, gid):
            if path == target and mode == "w":
                return 0
            return errno.EACCES

        h.access_fn = access_fn
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
        finally:
            h.stop()

    def test_missing_pool_executable_fails(self):
        h = Harness(self).start()
        h.missing_paths = {os.path.join(pool_dir(1), "runsvc.sh")}
        try:
            self.assertEqual(run_quiet_main(BASE), 1)
            self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_fork_setup_failure_is_999(self):
        def boom():
            raise OSError(errno.EPERM, "demotion failed")
        self.assertEqual(probe._forked_check(boom), 999)

    def test_smoke_invocation_uses_input_not_stdin(self):
        seen = {}

        class R:
            returncode = 0

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            seen["kw"] = kw
            return R()

        with mock.patch.object(probe.subprocess, "run",
                               side_effect=fake_run):
            rc = probe.run_smoke_under_service(1234, 1002, 1002, HOME,
                                               "/usr/lib/postgresql/16/bin")
        self.assertEqual(rc, 0)
        self.assertIn("input", seen["kw"])
        self.assertNotIn("stdin", seen["kw"])
        cmd = seen["cmd"]
        for flag in ("--no-new-privs", "--bounding-set=-all",
                     "--inh-caps=-all", "--ambient-caps=-all"):
            self.assertIn(flag, cmd)
        self.assertIn("--target=1234", cmd)
        self.assertLessEqual(seen["kw"]["timeout"], 180)
        self.assertEqual(seen["kw"]["env"],
                         {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})

    def test_run_quiet_env_sanitized(self):
        seen = {}

        class R:
            returncode = 0
            stdout = b""
            stderr = b""

        def fake_run(argv, **kw):
            seen.update(kw)
            return R()

        with mock.patch.object(probe.subprocess, "run",
                               side_effect=fake_run):
            probe.run_quiet(["true"])
        self.assertEqual(seen["env"],
                         {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})


class ReviewRegressionTests(unittest.TestCase):
    def run_harness(self, configure, expected):
        h = Harness(self).start()
        try:
            configure(h)
            self.assertEqual(run_quiet_main(BASE), expected)
            if expected:
                self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_missing_effective_override_fields_are_not_empty(self):
        for key in ('DropInPaths', 'EnvironmentFiles', 'ExecStart', 'FragmentPath', 'InaccessiblePaths'):
            with self.subTest(key=key):
                self.run_harness(lambda h: h.props.pop(key), 1)

    def test_execstart_extra_arguments_and_second_command_refused(self):
        for value in (EXEC_SHAPE.replace('argv[]=' + TREE + '/runsvc.sh',
                                         'argv[]=' + TREE + '/runsvc.sh --unexpected'),
                      EXEC_SHAPE + ' ' + EXEC_SHAPE):
            with self.subTest(value=value):
                self.run_harness(lambda h: h.props.update(ExecStart=value), 1)

    def test_optional_mask_rendering_passes(self):
        self.run_harness(lambda h: h.props.update(
            InaccessiblePaths='-/run/docker.sock -/var/run/docker.sock -/run/containerd'), 0)

    def test_each_tcp_listener_needs_own_positive_control(self):
        h = Harness(self).start()
        h.tcp_listeners = [('LAN', '192.0.2.76', 9999), ('Tailscale', '192.0.2.96', 4567)]
        try:
            with mock.patch.object(probe, 'connect_tcp_errno_as_uid', side_effect=
                                   lambda host, port, uid, gid: errno.ECONNREFUSED if port == 4567
                                   else (0 if uid == 1001 else errno.EACCES)):
                self.assertEqual(run_quiet_main(BASE), 1)
                self.assertIn('Docker listener positive control failed for Tailscale', probe.failures)
        finally:
            h.stop()

    def test_each_unix_listener_needs_own_positive_control(self):
        h = Harness(self).start()
        h.unix_sockets = ['/run/unusual/docker.sock']
        try:
            with mock.patch.object(probe, 'connect_errno_as_uid', side_effect=
                                   lambda path, uid, gid: errno.ECONNREFUSED if 'unusual' in path
                                   else (0 if uid == 1001 else errno.EACCES)):
                self.assertEqual(run_quiet_main(BASE), 1)
                self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_unix_dead_daemon_or_failed_demotion_is_not_denial(self):
        for code in (errno.ECONNREFUSED, errno.ENOENT, 999):
            with self.subTest(code=code):
                self.run_harness(lambda h: setattr(h, 'connect_code', code), 1)

    def test_unsupported_abstract_docker_listener_refuses(self):
        self.run_harness(lambda h: setattr(h, 'unix_sockets', ['@docker-fixture']), 1)

    def test_listener_ownership_unreadable_refuses(self):
        h = Harness(self).start()
        try:
            with mock.patch.object(probe, 'docker_socket_inodes', return_value=None):
                self.assertEqual(run_quiet_main(BASE), 1)
                self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_pool_mainpid_actual_uid_checked(self):
        h = Harness(self).start()
        try:
            with mock.patch.object(probe, 'read_proc_status', return_value=healthy_status()):
                self.assertEqual(run_quiet_main(BASE), 1)
                self.assertEqual(h.smoke_calls, 0)
        finally:
            h.stop()

    def test_process_enumeration_unreadable_is_not_empty_success(self):
        with mock.patch.object(probe.os, 'listdir', return_value=['123']), \
                mock.patch.object(probe, 'read_proc_status', return_value={}), \
                mock.patch.object(probe.os.path, 'exists', return_value=True):
            self.assertIsNone(probe.list_pids_of_uid(1001))

    def test_real_symlink_ancestry_includes_lexical_and_resolved_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'trusted').mkdir()
            (root / 'shared' / 'nested').mkdir(parents=True)
            (root / 'shared' / 'nested' / 'runsvc.sh').write_text('fixture')
            (root / 'trusted' / 'link').symlink_to(root / 'shared' / 'nested')
            chain = probe.executable_ancestors(str(root / 'trusted/link/runsvc.sh'))
            for p in ('trusted', 'trusted/link', 'shared', 'shared/nested'):
                self.assertIn(str(root / p), chain)
            self.assertIn('/', chain)

    def test_dangling_executable_symlink_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = pathlib.Path(tmp) / 'link'
            link.symlink_to(pathlib.Path(tmp) / 'missing')
            with self.assertRaises(OSError):
                probe.executable_ancestors(str(link))


class ListenerInventoryTests(unittest.TestCase):
    def test_tcp_tables_cover_lan_tailscale_wildcard_ipv6_and_nonstandard_ports(self):
        def row(address, port, inode, state='0A'):
            return '0: %s:%04X 00000000:0000 %s 0:0 0:0 0 1001 0 %s\n' % (address, port, state, inode)
        tables = {
            '/proc/net/tcp': 'header\n' + row('0900000A', 9999, 100)
                + row('01004064', 4567, 101) + row('00000000', 5555, 102)
                + row('0100007F', 22, 999) + row('0100007F', 6000, 103, '01'),
            '/proc/net/tcp6': 'header\n' + row('00000000000000000000000001000000', 8888, 104),
        }
        with mock.patch('builtins.open', side_effect=lambda p: io.StringIO(tables[p])):
            rows = probe.listening_tcp_endpoints({'100', '101', '102', '103', '104'})
        self.assertEqual([(host, port) for _, host, port in rows],
                         [('192.0.2.39', 9999), ('192.0.2.96', 4567), ('127.0.0.1', 5555), ('::1', 8888)])

    def test_one_unreadable_tcp_table_fails_closed(self):
        def opened(path):
            if path.endswith('tcp6'):
                raise PermissionError('fixture')
            return io.StringIO('header\n')
        with mock.patch('builtins.open', side_effect=opened):
            self.assertIsNone(probe.listening_tcp_endpoints(set()))

    def test_malformed_owned_tcp_address_refuses(self):
        table = 'header\n0: NOTHEX:2710 0:0 0A 0:0 0:0 0 1001 0 123\n'
        with mock.patch('builtins.open', return_value=io.StringIO(table)):
            self.assertIsNone(probe.listening_tcp_endpoints({'123'}))

    def test_unix_inventory_filters_listening_streams_not_names(self):
        table = ('header\n'
                 '0: 2 0 00010000 0001 01 123 /run/custom/control.sock\n'
                 '0: 2 0 00010000 0001 01 124 @abstract-docker\n'
                 '0: 2 0 00000000 0001 03 125 /run/connected.sock\n'
                 '0: 2 0 00010000 0001 01 999 /run/unrelated.sock\n')
        with mock.patch('builtins.open', return_value=io.StringIO(table)):
            self.assertEqual(probe.listening_unix_sockets({'123', '124', '125'}),
                             ['/run/custom/control.sock', '@abstract-docker'])

    def test_unnamed_listening_unix_socket_refuses(self):
        with mock.patch('builtins.open', return_value=io.StringIO(
                'header\n0: 2 0 00010000 0001 01 123\n')):
            self.assertIsNone(probe.listening_unix_sockets({'123'}))

    def test_fd_ownership_covers_rootful_and_all_docker_uid_slots(self):
        statuses = {str(i + 1): 'Uid: ' + ' '.join('1001' if j == i else '0' for j in range(4))
                    for i in range(4)}
        statuses.update({'5': 'Uid: 0 0 0 0', '6': 'Uid: 0 0 0 0', '7': 'Uid: 0 0 0 0'})
        def listdir(path):
            return list(statuses) if path == '/proc' else ['3', '4']
        def readlink(path):
            pid = path.split('/')[2]
            if path.endswith('/exe'):
                return '/usr/bin/' + {'5': 'dockerd', '6': 'rootlesskit', '7': 'sshd'}.get(pid, 'fixture')
            return 'socket:[%s]' % pid if path.endswith('/3') else '/dev/null'
        with mock.patch.object(probe.os, 'listdir', side_effect=listdir), \
                mock.patch.object(probe.os, 'readlink', side_effect=readlink), \
                mock.patch('builtins.open', side_effect=lambda p: io.StringIO(statuses[p.split('/')[2]])):
            self.assertEqual(probe.docker_socket_inodes(1001), {'1', '2', '3', '4', '5', '6'})

    def test_unreadable_fd_inventory_refuses(self):
        with mock.patch.object(probe.os, 'listdir', side_effect=PermissionError('fixture')):
            self.assertIsNone(probe.docker_socket_inodes(1001))


class _Completed:
    def __init__(self, returncode=0, stdout=b""):
        self.returncode = returncode
        self.stdout = stdout


class _FakeServer:
    """Mock for the foreground postgres Popen handle in SMOKE_SCRIPT."""

    def __init__(self, poll_vals=None, wait_ret=0, wait_first_exc=None):
        self.pid = 424242
        self._poll_vals = list(poll_vals) if poll_vals else [None]
        self._wait_ret = wait_ret
        self._wait_first_exc = wait_first_exc
        self.poll_calls = 0
        self.signals = []
        self.wait_calls = []
        self.wait_count = 0

    def poll(self):
        self.poll_calls += 1
        if len(self._poll_vals) > 1:
            return self._poll_vals.pop(0)
        return self._poll_vals[0]

    def send_signal(self, sig):
        self.signals.append(sig)

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        self.wait_count += 1
        if self.wait_count == 1 and self._wait_first_exc is not None:
            raise self._wait_first_exc
        return self._wait_ret


def _exec_smoke(run_effect, popen_effect):
    """Exec the REAL probe.SMOKE_SCRIPT with mocked subprocess/signal/time.

    Real temp dirs under /tmp only; no services, privileges, network, or PG.
    Returns (exit_code, smoke_dir_exists, rmtree_calls, popen_calls, server).
    """
    outer = tempfile.mkdtemp(prefix="smoke-test-outer-")
    inner = os.path.join(outer, "smoke-d")
    os.mkdir(inner, 0o700)
    real_rmtree = shutil.rmtree
    rmtree_calls = []
    popen_calls = []
    servers = []

    def fake_rmtree(p, *a, **k):
        rmtree_calls.append(p)
        return real_rmtree(p, *a, **k)

    def fake_popen(argv, *a, **k):
        popen_calls.append(list(argv))
        srv = popen_effect(argv, *a, **k)
        servers.append(srv)
        return srv

    monotonic_vals = [1000.0 + i * 0.1 for i in range(500)]
    with mock.patch.object(tempfile, "mkdtemp", return_value=inner), \
         mock.patch.object(signal, "signal", return_value=None), \
         mock.patch.object(signal, "alarm", return_value=0, create=True), \
         mock.patch.object(subprocess, "run", side_effect=run_effect), \
         mock.patch.object(subprocess, "Popen", side_effect=fake_popen), \
         mock.patch.object(time, "sleep", return_value=None), \
         mock.patch.object(time, "monotonic", side_effect=monotonic_vals), \
         mock.patch.object(shutil, "rmtree", side_effect=fake_rmtree), \
         mock.patch.object(os, "killpg",
                            side_effect=AssertionError("killpg must not run")), \
         mock.patch.object(sys, "argv", ["smoke", "/fake/pgbin"]):
        try:
            exec(probe.SMOKE_SCRIPT, {"__name__": "__smoke__"})
            code = None
        except SystemExit as e:
            code = e.code
    exists = os.path.exists(inner)
    try:
        real_rmtree(outer, ignore_errors=True)
    except Exception:
        pass
    srv = servers[0] if servers else None
    return code, exists, rmtree_calls, popen_calls, srv


def _healthy_run(pg_isready_rc=0, psql_out=b"1\n", psql_exc=None):
    def effect(argv, *a, **k):
        cmd = argv[0] if argv else ""
        base = os.path.basename(cmd) if isinstance(cmd, str) else ""
        if base == "pg_isready":
            return _Completed(returncode=pg_isready_rc)
        if base == "psql":
            if psql_exc is not None:
                raise psql_exc
            return _Completed(returncode=0, stdout=psql_out)
        return _Completed(returncode=0, stdout=b"")
    return effect


class SmokeScriptTests(unittest.TestCase):
    def test_successful_path_sigint_wait0_removes(self):
        srv = _FakeServer(poll_vals=[None], wait_ret=0)
        code, exists, rmtree_calls, popen_calls, got = _exec_smoke(
            _healthy_run(), lambda *a, **k: srv)
        self.assertEqual(code, 0)
        self.assertFalse(exists)
        self.assertEqual(len(rmtree_calls), 1)
        # Exact temporary server gets fast SIGINT shutdown and wait(20).
        self.assertEqual(got.signals, [signal.SIGINT])
        self.assertEqual(got.wait_calls, [20])
        # Foreground postgres under the temp dir, private socket, no TCP.
        flat = " ".join(popen_calls[0])
        self.assertIn("postgres", flat)
        self.assertIn("-k", flat)
        self.assertNotIn("pg_ctl", flat)
        self.assertNotIn("2375", flat)

    def test_sql_timeout_still_cleans_server(self):
        srv = _FakeServer(poll_vals=[None], wait_ret=0)
        exc = subprocess.TimeoutExpired(cmd="psql", timeout=15)
        code, exists, _, _, got = _exec_smoke(
            _healthy_run(psql_exc=exc), lambda *a, **k: srv)
        self.assertEqual(code, 10)
        self.assertFalse(exists)
        self.assertEqual(got.signals, [signal.SIGINT])
        self.assertEqual(got.wait_calls, [20])

    def test_sql_wrong_answer_cleans_server(self):
        srv = _FakeServer(poll_vals=[None], wait_ret=0)
        code, exists, _, _, got = _exec_smoke(
            _healthy_run(psql_out=b"0\n"), lambda *a, **k: srv)
        self.assertEqual(code, 14)
        self.assertFalse(exists)
        self.assertEqual(got.signals, [signal.SIGINT])

    def test_startup_not_ready_cleans_server(self):
        srv = _FakeServer(poll_vals=[None], wait_ret=0)
        code, exists, _, _, got = _exec_smoke(
            _healthy_run(pg_isready_rc=1), lambda *a, **k: srv)
        self.assertEqual(code, 10)
        self.assertFalse(exists)
        self.assertEqual(got.signals, [signal.SIGINT])

    def test_server_exited_early_no_sigint_but_waits(self):
        srv = _FakeServer(poll_vals=[0], wait_ret=0)
        code, exists, _, _, got = _exec_smoke(
            _healthy_run(), lambda *a, **k: srv)
        self.assertEqual(code, 10)
        self.assertFalse(exists)
        self.assertEqual(got.signals, [])
        self.assertEqual(got.wait_calls, [20])

    def test_wait_nonzero_fails_cleanup_preserves_dir(self):
        srv = _FakeServer(poll_vals=[None], wait_ret=1)
        code, exists, rmtree_calls, _, got = _exec_smoke(
            _healthy_run(), lambda *a, **k: srv)
        self.assertEqual(code, 20)
        self.assertTrue(exists)
        self.assertEqual(rmtree_calls, [])
        self.assertEqual(got.wait_calls, [20])

    def test_wait_raises_fallback_sigquit_preserves_dir(self):
        srv = _FakeServer(poll_vals=[None], wait_ret=0,
                          wait_first_exc=RuntimeError("wait timeout"))
        code, exists, rmtree_calls, _, got = _exec_smoke(
            _healthy_run(), lambda *a, **k: srv)
        self.assertEqual(code, 20)
        self.assertTrue(exists)
        self.assertEqual(rmtree_calls, [])
        self.assertEqual(got.signals, [signal.SIGINT, signal.SIGQUIT])
        self.assertEqual(got.wait_calls, [20, 5])

    def test_build_failure_removes_without_server(self):
        def effect(argv, *a, **k):
            cmd = argv[0] if argv else ""
            if cmd == "cc":
                return _Completed(returncode=1)
            return _Completed(returncode=0, stdout=b"")

        code, exists, rmtree_calls, popen_calls, got = _exec_smoke(
            effect, lambda *a, **k: self.fail("Popen must not run"))
        self.assertEqual(code, 10)
        self.assertFalse(exists)
        self.assertEqual(len(rmtree_calls), 1)
        self.assertEqual(popen_calls, [])
        self.assertIsNone(got)

    def test_script_never_killpg_or_sigkill(self):
        self.assertNotIn("killpg", probe.SMOKE_SCRIPT)
        # No code use of SIGKILL (the word may appear in a "never SIGKILL"
        # comment); fallback is SIGQUIT on the exact temporary server only.
        self.assertNotIn("signal.SIGKILL", probe.SMOKE_SCRIPT)
        self.assertNotIn("os.killpg", probe.SMOKE_SCRIPT)
        self.assertIn("signal.SIGQUIT", probe.SMOKE_SCRIPT)
        self.assertIn("start_new_session=True", probe.SMOKE_SCRIPT)


if __name__ == "__main__":
    unittest.main()
