#!/usr/bin/env python3
"""Isolated-runner acceptance probe (case_4c57f7c0).

Non-destructive, credential-free, root-orchestrated. Effective systemd
properties plus live MainPID /proc identity; UID boundary proven outside the
mount namespace via real downgraded connect/access; five explicit pool units
whose credentials live in EACH pool WorkingDirectory; REAL cc + PostgreSQL
smoke under the service mount namespace.

Never prints tokens, environments, or subprocess output.
Property semantics: systemd v255 `systemctl show`. Unit layout: GitHub
self-hosted runner `runsvc.sh` as a systemd `Type=simple` service.
"""
import argparse
import errno
import hashlib
import os
import pwd
import re
import socket
import subprocess
import sys

SYSTEMD_DIR = "/etc/systemd/system"
EXPECTED = {
    "NoNewPrivileges": "yes",
    "CapabilityBoundingSet": "",
    "AmbientCapabilities": "",
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "PrivateTmp": "yes",
    "ProtectProc": "invisible",
    "RestrictSUIDSGID": "yes",
    "RestrictNamespaces": "yes",
    "LockPersonality": "yes",
    "RestrictRealtime": "yes",
    "UMask": "0077",
}
SAFE_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}
DEFAULT_DOCKER_USER = "gha-runner"
DEFAULT_DOCKER_HOME = "/home/gha-runner"
DEFAULT_PG_BIN_DIR = "/usr/lib/postgresql/16/bin"
ROOTFUL_SOCKETS = ("/run/docker.sock", "/var/run/docker.sock")
# Well-known container API ports worth proving closed even when no local
# listener row mentions them (defense in depth, not a substitute for the
# enumerated-listener checks below).
PROBE_ONLY_TCP_PORTS = (2375, 2376)
REQUIRED_SECRETS = (".runner", ".credentials", ".credentials_rsaparams")
OPTIONAL_SECRETS = (".env",)

failures = []


def fail(msg):
    failures.append(msg)
    print("FAIL: %s" % msg)


def ok(msg):
    print("PASS: %s" % msg)


def run_quiet(argv, preexec=None, timeout=60):
    try:
        p = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, preexec_fn=preexec, timeout=timeout,
            env=dict(SAFE_ENV))
        return p.returncode
    except Exception:
        return 127


def demote_fn(uid, gid):
    def _d():
        # Setup failure must propagate (nonzero preexec), never be ignored.
        os.setgroups([gid])
        os.setgid(gid)
        os.setuid(uid)
    return _d


def systemctl_show(service, props):
    argv = ["systemctl", "show", service, "--property=" + ",".join(props)]
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=30, env=dict(SAFE_ENV))
    except Exception:
        return {}
    if p.returncode != 0:
        return {}
    out = {}
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_exec_path(exec_start):
    """Parse the binary path from systemctl's structured ExecStart string.

    Real shape: { path=/opt/.../run.sh ; argv[]=/opt/.../run.sh ; ... }
    Returns "" when the shape is absent or malformed.
    """
    m = re.search(r"path=([^ ;}]+)", exec_start or "")
    return m.group(1) if m else ""


def expected_paths(service_name, isolated_user):
    """Exact tree/home for this listener; "" runner when unparseable."""
    prefix, suffix = "actions.runner.", ".service"
    runner = ""
    if service_name.startswith(prefix) and service_name.endswith(suffix):
        rest = service_name[len(prefix):-len(suffix)]
        parts = rest.split(".")
        if len(parts) >= 2:
            runner = ".".join(parts[1:])
    tree = "/opt/two-isolated/" + runner if runner else ""
    home = "/var/lib/" + isolated_user
    return tree, home, runner


def read_proc_status(pid):
    try:
        with open("/proc/%d/status" % pid) as h:
            fields = {}
            for line in h:
                if ":" in line:
                    k, v = line.split(":", 1)
                    fields[k.strip()] = v.strip()
            return fields
    except Exception:
        return {}


def list_pids_of_uid(uid):
    """PIDs where the uid appears in ANY real/effective/saved/fs slot.

    Matching only the real UID lets an effective/saved/filesystem Docker-UID
    process escape every signal/memory/fd check below.
    """
    found = []
    want = str(uid)
    try:
        entries = os.listdir("/proc")
    except Exception:
        return None
    for e in entries:
        if not e.isdigit():
            continue
        st = read_proc_status(int(e))
        parts = (st.get("Uid", "") or "").split()
        if len(parts) != 4:
            if os.path.exists('/proc/' + e):
                return None
            continue
        if want in parts:
            found.append(int(e))
    return found


def _forked_check(fn):
    """Run fn() in a forked child after caller demotes; return child int."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            code = fn()
            os.write(w, str(int(code)).encode())
        except Exception:
            # Setup/demotion failure is NOT EPERM from the operation tested.
            try:
                os.write(w, b"999")
            except Exception:
                pass
        os._exit(0)
    os.close(w)
    os.waitpid(pid, 0)
    with os.fdopen(r, "rb") as f:
        data = f.read()
    try:
        return int((data.decode() or "999").strip())
    except Exception:
        return 999


def connect_errno_as_uid(path, uid, gid):
    def _c():
        os.setgroups([gid])
        os.setgid(gid)
        os.setuid(uid)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        try:
            s.connect(path)
        except OSError as e:
            return e.errno or 999
        finally:
            s.close()
        return 0
    return _forked_check(_c)


_AMODE = {"r": os.R_OK, "w": os.W_OK, "x": os.X_OK}


def access_as_uid(path, mode, uid, gid):
    amode = _AMODE[mode]
    try:
        os.stat(path)  # Absence/inaccessible root-side path is not denial.
    except OSError:
        return 999

    def _c():
        os.setgroups([gid])
        os.setgid(gid)
        os.setuid(uid)
        if os.access(path, amode):
            return 0
        return errno.EACCES
    return _forked_check(_c)


def signal0_errno_as_uid(target_pid, uid, gid):
    def _c():
        os.setgroups([gid])
        os.setgid(gid)
        os.setuid(uid)
        try:
            os.kill(target_pid, 0)
            return 0
        except OSError as e:
            return e.errno or 999
    return _forked_check(_c)


def _hex_ip_to_str(addr_hex, family):
    """Decode a /proc/net/tcp{,6} address field to a printable IP.

    IPv4 appears as one little-endian 32-bit word (0100007F = 127.0.0.1);
    IPv6 as four little-endian 32-bit words. Returns "" when undecodable.
    """
    try:
        raw = bytes.fromhex(addr_hex)
    except ValueError:
        return ""
    try:
        if family == "tcp":
            if len(raw) != 4:
                return ""
            return socket.inet_ntoa(raw[::-1])
        if len(raw) != 16:
            return ""
        words = [raw[i:i + 4][::-1] for i in range(0, 16, 4)]
        return socket.inet_ntop(socket.AF_INET6, b"".join(words))
    except Exception:
        return ""


def docker_socket_inodes(uid):
    """Map Docker listener fds, independent of port, bind address or socket path.

    Include every UID slot and rootful/rootless daemon/port-forwarder processes.
    Unreadable live process metadata is not an empty inventory. /proc/net uses
    socket inodes, not filesystem socket inodes (proc_pid_fd(5), proc_net(5)).
    """
    found = set()
    try:
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            base = '/proc/' + entry
            try:
                with open(base + '/status') as handle:
                    fields = dict(line.split(':', 1) for line in handle if ':' in line)
                slots = fields['Uid'].split()
                if len(slots) != 4:
                    return None
                try:
                    exe = os.path.basename(os.readlink(base + '/exe'))
                except FileNotFoundError:
                    exe = ''  # Kernel threads have no executable.
                if str(uid) not in slots and exe not in (
                        'dockerd', 'rootlesskit', 'docker-proxy', 'slirp4netns', 'vpnkit'):
                    continue
                for fd in os.listdir(base + '/fd'):
                    try:
                        link = os.readlink(base + '/fd/' + fd)
                    except FileNotFoundError:
                        continue
                    match = re.fullmatch(r'socket:\[(\d+)\]', link)
                    if match:
                        found.add(match.group(1))
            except FileNotFoundError:
                continue  # Process exited; required MainPIDs are checked separately.
    except (OSError, KeyError):
        return None
    return found


def listening_tcp_endpoints(inodes):
    """Docker-owned TCP LISTEN sockets, including LAN/Tailscale/custom ports.

    Correlate every host-network listener's inode with daemon/forwarder fds;
    do not mistake sshd's failed `docker version` for a proven Docker denial.
    Missing IPv6 tables are allowed only when the kernel has no IPv6 support.
    """
    found = []
    readable = False
    for path, family in (("/proc/net/tcp", "tcp"),
                         ("/proc/net/tcp6", "tcp6")):
        try:
            with open(path) as h:
                lines = h.read().splitlines()[1:]
        except FileNotFoundError:
            if family == 'tcp6' and not socket.has_ipv6:
                continue
            return None
        except OSError:
            return None
        readable = True
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                return None
            if parts[3] != "0A" or parts[9] not in inodes:
                continue
            addr_hex, _, port_hex = parts[1].rpartition(":")
            try:
                port = int(port_hex, 16)
            except ValueError:
                return None
            if not 0 < port < 65536:
                return None
            addr = _hex_ip_to_str(addr_hex, family)
            if not addr:
                return None
            if addr == "0.0.0.0":
                host = "127.0.0.1"
            elif addr == "::":
                host = "::1"
            else:
                host = addr
            found.append(("%s:%d (%s)" % (addr, port, path), host, port))
    if not readable:
        return None
    return found


def listening_unix_sockets(inodes):
    """Docker-owned stream listeners from /proc/net/unix, including abstract.

    Never silently skip unsupported addresses: abstract/unnamed listeners
    fail the positive Docker control rather than being certified as absent.
    """
    try:
        with open("/proc/net/unix") as h:
            lines = h.read().splitlines()[1:]
    except OSError:
        return None
    found = []
    for line in lines:
        parts = line.split(maxsplit=7)
        if len(parts) < 7:
            return None
        if parts[6] not in inodes or parts[3] != '00010000' or parts[4] != '0001':
            continue
        if len(parts) != 8:
            return None
        path = parts[7]
        if path not in found:
            found.append(path)
    return found


def connect_tcp_errno_as_uid(host, port, uid, gid):
    def _c():
        os.setgroups([gid])
        os.setgid(gid)
        os.setuid(uid)
        try:
            s = socket.create_connection((host, port), timeout=5)
        except OSError as e:
            return e.errno or 999
        try:
            s.close()
        except Exception:
            pass
        return 0
    return _forked_check(_c)


def read_unit_bytes(path):
    """Hex sha256 of the live unit file, or "" when unreadable."""
    try:
        with open(path, "rb") as h:
            return hashlib.sha256(h.read()).hexdigest()
    except OSError:
        return ""


def check_container_tcp(display, host, port, dock, iso):
    """Require a live positive connect control and an actual UID denial.

    Socket ownership establishes the candidate, not failed HTTP fingerprinting.
    This also covers non-HTTP runtime control sockets and TLS endpoints without
    mistaking a protocol error or missing client credential for isolation.
    """
    if connect_tcp_errno_as_uid(host, port, dock.pw_uid, dock.pw_gid) != 0:
        fail("Docker listener positive control failed for %s" % display)
        return
    denied = connect_tcp_errno_as_uid(host, port, iso.pw_uid, iso.pw_gid)
    if denied in (errno.EACCES, errno.EPERM):
        ok("Docker identity succeeds and isolated identity is denied at %s" % display)
    else:
        fail("Docker listener is reachable or denial is inconclusive for %s" % display)


def check_container_unix(path, dock, iso):
    if not path.startswith('/'):
        fail("unsupported Docker Unix listener address; boundary unproven")
        return
    if connect_errno_as_uid(path, dock.pw_uid, dock.pw_gid) != 0:
        fail("Docker socket positive control failed for %s" % path)
        return
    denied = connect_errno_as_uid(path, iso.pw_uid, iso.pw_gid)
    if denied in (errno.EACCES, errno.EPERM):
        ok("Docker identity succeeds and isolated identity is denied at %s" % path)
    else:
        fail("Docker socket is reachable or denial is inconclusive for %s" % path)


SMOKE_SCRIPT = r'''
import os, shutil, signal, subprocess, sys, tempfile, time
pgbin = sys.argv[1]
d = tempfile.mkdtemp(prefix="isolated-smoke-")
data = os.path.join(d, "pgdata")
server = None
cleanup_ok = True
result = 0

def interrupted(signum, frame):
    raise RuntimeError("smoke interrupted")

def checked(argv, timeout):
    p = subprocess.run(argv, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError("smoke command failed")
    return p.stdout

signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGALRM, interrupted)
signal.alarm(140)  # Reserve 25 seconds for cleanup before the outer 175s limit.
try:
    c, b = os.path.join(d, "t.c"), os.path.join(d, "t")
    with open(c, "w") as f:
        f.write("int main(void){return 0;}\n")
    checked(["cc", c, "-o", b], 20)
    checked([b], 10)
    checked([os.path.join(pgbin, "initdb"), "-D", data, "-U", "postgres", "-E", "UTF8"], 30)
    sock = os.path.join(d, "socks")
    os.mkdir(sock, 0o700)
    # Hold the foreground server directly: a pg_ctl timeout can leave startup
    # running after pg_ctl exits, making status=3 insufficient cleanup evidence.
    server = subprocess.Popen([os.path.join(pgbin, "postgres"), "-D", data,
                               "-k", sock, "-c", "listen_addresses="],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
    deadline = time.monotonic() + 20
    while True:
        if server.poll() is not None:
            raise RuntimeError("temporary postgres exited")
        ready = subprocess.run([os.path.join(pgbin, "pg_isready"), "-h", sock, "-U", "postgres"],
                               capture_output=True, timeout=2)
        if ready.returncode == 0:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("temporary postgres not ready")
        time.sleep(0.2)
    answer = checked([os.path.join(pgbin, "psql"), "-h", sock, "-U", "postgres", "-tAc", "select 1"], 15)
    if answer.strip() != b"1":
        result = 14
except BaseException:
    result = 10
finally:
    signal.alarm(0)
    # Fast shutdown and wait on the exact process we started, even on timeout.
    if server is not None:
        try:
            if server.poll() is None:
                server.send_signal(signal.SIGINT)
            if server.wait(timeout=20) != 0:
                cleanup_ok = False
        except Exception:
            cleanup_ok = False
            try:
                # Immediate shutdown of only our temporary server lets postgres
                # release its IPC resources; never SIGKILL or a live service PID.
                server.send_signal(signal.SIGQUIT)
                server.wait(timeout=5)
            except Exception:
                pass
    if cleanup_ok:
        try:
            shutil.rmtree(d)
        except Exception:
            cleanup_ok = False
sys.exit(result if cleanup_ok else 20)
'''


def run_smoke_under_service(main_pid, uid, gid, home, pg_bin):
    cmd = ["nsenter", "--target=%d" % main_pid, "--mount",
           "setpriv", "--reuid=%d" % uid, "--regid=%d" % gid,
           "--clear-groups", "--no-new-privs",
           "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all",
           "env", "-i",
           "HOME=%s" % home, "PATH=/usr/bin:/bin",
           "python3", "-", pg_bin]
    try:
        p = subprocess.run(cmd, input=SMOKE_SCRIPT.encode(),
                           stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=175,
                           env=dict(SAFE_ENV))
        return p.returncode
    except Exception:
        return 127


def effective_home(env):
    for tok in (env or "").split():
        if tok.startswith("HOME="):
            return tok[5:]
    return ""


def parse_args(argv):
    for a in argv:
        if a == "--no-native-pg" or a.startswith("--no-native-pg="):
            sys.stderr.write("REFUSED: --no-native-pg is not accepted\n")
            sys.exit(2)
        if a in ("--skip", "--no-check", "--dry-run", "--allow-shared-identity") \
                or a.startswith("--skip"):
            sys.stderr.write("REFUSED: skip flags are not accepted\n")
            sys.exit(2)
    ap = argparse.ArgumentParser(prog="isolated-acceptance-probe")
    ap.add_argument("--service-name", required=True)
    ap.add_argument("--isolated-user", required=True)
    ap.add_argument("--docker-user", default=DEFAULT_DOCKER_USER)
    ap.add_argument("--docker-home", default=DEFAULT_DOCKER_HOME)
    ap.add_argument("--pool-service", action="append", default=[])
    ap.add_argument("--pg-bin-dir", default=DEFAULT_PG_BIN_DIR)
    ap.add_argument("--unit-sha256", default=None,
                    help="journaled candidate unit bytes; when given, the live "
                         "unit file must hash to exactly this digest")
    ns = ap.parse_args(argv)
    targets = {"actions.runner.TogetherWeOwn.coolify-vps-%d.service" % n:
               "gha-isolated-%d" % n for n in (6, 7)}
    if targets.get(ns.service_name) != ns.isolated_user:
        sys.stderr.write("REFUSED: target must be runner 6 or 7 and its matching isolated account\n")
        sys.exit(2)
    if ns.docker_user != DEFAULT_DOCKER_USER:
        sys.stderr.write("REFUSED: Docker identity must be gha-runner\n")
        sys.exit(2)
    if len(ns.pool_service) != 5:
        sys.stderr.write("REFUSED: exactly five --pool-service units are required\n")
        sys.exit(2)
    if len(set(ns.pool_service)) != 5:
        sys.stderr.write("REFUSED: --pool-service units must be distinct\n")
        sys.exit(2)
    pool = {"actions.runner.TogetherWeOwn.coolify-vps-%d.service" % n for n in range(1, 6)}
    if set(ns.pool_service) != pool:
        sys.stderr.write("REFUSED: pool must be exactly the existing runners 1 through 5\n")
        sys.exit(2)
    for p in (ns.docker_home, ns.pg_bin_dir):
        if not p.startswith("/"):
            sys.stderr.write("REFUSED: paths must be absolute\n")
            sys.exit(2)
    if ns.unit_sha256 is None:
        sys.stderr.write("REFUSED: --unit-sha256 is required\n")
        sys.exit(2)
    if not re.fullmatch(r"[0-9a-f]{64}", ns.unit_sha256):
        sys.stderr.write("REFUSED: --unit-sha256 must be 64 lowercase hex\n")
        sys.exit(2)
    return ns


def executable_ancestors(path):
    """Check both lexical replacement paths and every resolved symlink chain."""
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        raise ValueError('unsafe executable path')
    chain = set()
    cursor = path
    while True:
        chain.add(cursor)
        os.stat(cursor)  # Refuse dangling/looped links on Python 3.9 too.
        resolved = os.path.realpath(cursor)
        while resolved not in chain:
            chain.add(resolved)
            resolved = os.path.dirname(resolved)
        if cursor == '/':
            break
        cursor = os.path.dirname(cursor)
    return sorted(chain)


def deny_check(path, mode, uid, gid, breach_msg, ok_msg, inconc_msg):
    code = access_as_uid(path, mode, uid, gid)
    if code == 0:
        fail(breach_msg)
        return False
    if code in (errno.EACCES, errno.EPERM):
        ok(ok_msg)
        return True
    fail(inconc_msg)
    return False


def main(argv=None):
    failures[:] = []
    ns = parse_args(argv if argv is not None else sys.argv[1:])
    if os.geteuid() != 0:
        sys.stderr.write("REFUSED: probe must run as root\n")
        return 2
    try:
        iso = pwd.getpwnam(ns.isolated_user)
        dock = pwd.getpwnam(ns.docker_user)
    except KeyError:
        sys.stderr.write("REFUSED: unknown account\n")
        return 2
    if iso.pw_uid == 0 or ns.isolated_user == "root":
        fail("isolated account must be non-root")
    if iso.pw_uid == dock.pw_uid:
        fail("isolated and docker accounts share a uid")
    else:
        ok("isolated uid differs from docker uid")
    same = [e.pw_name for e in pwd.getpwall() if e.pw_uid == iso.pw_uid]
    if len(same) != 1:
        fail("isolated uid is not unique")
    else:
        ok("isolated uid is unique")
    try:
        groups = os.getgrouplist(ns.isolated_user, iso.pw_gid)
    except Exception:
        groups = None
    if groups != [iso.pw_gid]:
        fail("isolated account holds membership outside its primary group")
    else:
        ok("isolated account holds only its primary group")

    exp_tree, exp_home, runner = expected_paths(ns.service_name,
                                                ns.isolated_user)
    if not runner:
        fail("service name yields no runner identity")
        return finish(ns)
    rn = runner.rsplit("-", 1)[-1]
    un = ns.isolated_user.rsplit("-", 1)[-1]
    if rn in ("6", "7") and un in ("6", "7") and rn != un:
        fail("isolated account does not match the runner identity")

    props = systemctl_show(ns.service_name, [
        "LoadState", "ActiveState", "MainPID", "User", "Group",
        "NoNewPrivileges", "CapabilityBoundingSet", "AmbientCapabilities",
        "ProtectSystem", "ProtectHome", "PrivateTmp", "ProtectProc",
        "RestrictSUIDSGID", "RestrictNamespaces", "LockPersonality",
        "RestrictRealtime", "UMask", "SupplementaryGroups",
        "WorkingDirectory", "ReadWritePaths", "Environment",
        "ExecStart", "FragmentPath", "DropInPaths", "EnvironmentFiles",
        "InaccessiblePaths"])
    if not props:
        fail("service properties unreadable")
        return finish(ns)
    if props.get("LoadState") != "loaded":
        fail("service is not loaded")
    else:
        ok("service is loaded")
    if props.get("ActiveState") != "active":
        fail("service is not active")
        return finish(ns)
    ok("service is active")
    try:
        main_pid = int(props.get("MainPID", "0") or "0")
    except Exception:
        main_pid = 0
    if main_pid <= 0:
        fail("service has no MainPID")
        return finish(ns)
    if props.get("User") != ns.isolated_user:
        fail("effective User is not the isolated account")
    else:
        ok("effective service runs as the isolated account")
    if props.get("Group") != ns.isolated_user:
        fail("effective Group is not the isolated account")
    else:
        ok("effective service group is the isolated account")
    for k, want in EXPECTED.items():
        if k not in props:
            fail("effective %s is missing" % k)
        elif props[k] != want:
            fail("effective %s is not %s" % (k, want or "empty"))
    if all(k in props and props[k] == v for k, v in EXPECTED.items()):
        ok("effective unit carries the fixed hardening set")
    if (props.get("SupplementaryGroups", "") or "") != "":
        fail("effective service has supplementary groups")
    else:
        ok("effective service has no supplementary groups")
    env = props.get("Environment", "") or ""
    if "DOCKER_HOST=" in env or "CONTAINER_HOST=" in env:
        fail("effective service selects a container endpoint")
    else:
        ok("effective service selects no container endpoint")
    # --- Exact unit binding: live bytes plus the effective fields a
    # drop-in/override can change without touching the properties above ---
    # parse_args already refuses a missing/malformed digest, so the check
    # below is fail-closed: no path through acceptance leaves bytes unbound.
    live_digest = read_unit_bytes(os.path.join(SYSTEMD_DIR, ns.service_name))
    if not live_digest or live_digest != ns.unit_sha256:
        fail("live unit bytes differ from the journaled candidate")
    else:
        ok("live unit bytes match the journaled candidate")
    frag = props.get("FragmentPath", "") or ""
    if frag != os.path.join(SYSTEMD_DIR, ns.service_name):
        fail("effective FragmentPath is not the local system unit")
    else:
        ok("effective FragmentPath is the local system unit")
    if props.get("DropInPaths") != "":
        fail("effective DropInPaths must be empty; custom overrides require review")
    else:
        ok("effective unit carries no drop-in overrides")
    if props.get("EnvironmentFiles") != "":
        fail("effective EnvironmentFiles must be empty; custom env files require review")
    else:
        ok("effective unit loads no environment files")
    inac = {p.removeprefix('-') for p in (props.get("InaccessiblePaths", "") or "").split()}
    want_inac = {"/run/docker.sock", "/var/run/docker.sock", "/run/containerd"}
    if not want_inac.issubset(inac):
        fail("effective InaccessiblePaths omits a required daemon path")
    else:
        ok("effective unit masks the daemon socket paths")
    exec_start = props.get("ExecStart", "")
    paths = re.findall(r'(?:\{|;)\s*path=([^;]+);', exec_start)
    argvs = re.findall(r';\s*argv\[\]=([^;]+);', exec_start)
    want_exec = os.path.join(exp_tree, "runsvc.sh")
    if [p.strip() for p in paths] != [want_exec] or [a.strip() for a in argvs] != [want_exec]:
        fail("effective ExecStart is not exactly the isolated runsvc.sh without arguments")
    else:
        ok("effective ExecStart is the isolated runsvc.sh")
    workdir = props.get("WorkingDirectory", "") or ""
    if workdir != exp_tree:
        fail("effective WorkingDirectory is not the expected isolated tree")
    else:
        ok("effective WorkingDirectory is the expected isolated tree")
    rw_tokens = (props.get("ReadWritePaths", "") or "").split()
    if set(rw_tokens) != {exp_tree, exp_home} or len(rw_tokens) != 2:
        fail("unit writable paths are not exactly the isolated tree and home")
    else:
        ok("unit writable paths are exactly the isolated tree and home")
    home = effective_home(env)
    if home != exp_home:
        fail("effective HOME is not the expected isolated home")
    else:
        ok("effective HOME is the expected isolated home")

    st = read_proc_status(main_pid)
    if not st:
        fail("MainPID status unreadable")
        return finish(ns)
    uids = (st.get("Uid", "") or "").split()
    gids = (st.get("Gid", "") or "").split()
    if len(uids) != 4 or any(u != str(iso.pw_uid) for u in uids) or \
            len(gids) != 4 or any(g != str(iso.pw_gid) for g in gids):
        fail("MainPID real/effective/saved/filesystem UID/GID mismatch")
    else:
        ok("MainPID carries the isolated UID/GID in all four slots")
    if (st.get("Groups", "") or "").split() != [str(iso.pw_gid)]:
        fail("MainPID carries extra groups")
    else:
        ok("MainPID carries only the primary group")
    if (st.get("NoNewPrivs", "0") or "0") != "1":
        fail("MainPID lacks NoNewPrivs")
    else:
        ok("MainPID has NoNewPrivs")
    caps_ok = True
    for capkey in ("CapEff", "CapBnd", "CapAmb"):
        try:
            v = int(((st.get(capkey, "1") or "1").split() or ["1"])[0], 16)
        except Exception:
            v = 1
        if v != 0:
            fail("MainPID retains capabilities")
            caps_ok = False
            break
    if caps_ok:
        ok("MainPID holds no capabilities")

    # --- Socket boundary, outside the mount namespace --------------------
    docker_sock = "/run/user/%d/docker.sock" % dock.pw_uid
    if not os.path.exists(docker_sock):
        fail("rootless daemon socket is absent; positive control impossible")
    else:
        ok("rootless daemon socket exists for positive control")
        rc = run_quiet(["docker", "-H", "unix://" + docker_sock, "version",
                        "--format", "{{.Server.Version}}"],
                       preexec=demote_fn(dock.pw_uid, dock.pw_gid))
        if rc != 0:
            fail("positive control failed as the docker identity")
        else:
            ok("positive control answers as the docker identity")
        rc = run_quiet(["docker", "-H", "unix://" + docker_sock, "version",
                        "--format", "{{.Server.Version}}"],
                       preexec=demote_fn(iso.pw_uid, iso.pw_gid))
        if rc == 0:
            fail("isolated identity reached the daemon via explicit endpoint")
        else:
            ok("isolated identity denied at the explicit daemon endpoint")
    own_sock = "/run/user/%d/docker.sock" % iso.pw_uid
    if os.path.exists(own_sock):
        fail("a daemon socket exists for the isolated uid")
    else:
        ok("no daemon socket exists for the isolated uid")
    # Enumerate Docker daemon/UID/forwarder listener fds, not guessed ports.
    # Every candidate needs its OWN positive control and actual connect denial;
    # TLS errors, timeouts, exited daemons and CLI failures never mean isolation.
    inodes = docker_socket_inodes(dock.pw_uid)
    tcp_listeners = listening_tcp_endpoints(inodes) if inodes is not None else None
    unix_listeners = listening_unix_sockets(inodes) if inodes is not None else None
    if tcp_listeners is None or unix_listeners is None:
        fail("listener ownership/tables unreadable; container endpoints unproven")
    else:
        ok("enumerated %d TCP listener(s) and %d Unix socket(s)"
            % (len(tcp_listeners), len(unix_listeners)))
        for display, host, port in tcp_listeners:
            check_container_tcp(display, host, port, dock, iso)
        for path in unix_listeners:
            check_container_unix(path, dock, iso)
    for path in [docker_sock] + list(ROOTFUL_SOCKETS):
        if not os.path.exists(path):
            if path in ROOTFUL_SOCKETS:
                ok("rootful endpoint not exposed")
            else:
                fail("expected socket path is absent")
            continue
        check_container_unix(path, dock, iso)
    for port in PROBE_ONLY_TCP_PORTS:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.close()
            fail("TCP endpoint answers on localhost")
        except OSError as e:
            if e.errno == errno.ECONNREFUSED:
                ok("TCP endpoint is closed on localhost")
            else:
                fail("TCP endpoint check inconclusive")
        except Exception:
            fail("TCP endpoint check inconclusive")

    # --- Pool services: creds live in EACH pool WorkingDirectory ---------
    pool_main_pids = []
    for unit in ns.pool_service:
        pp = systemctl_show(unit, ["LoadState", "ActiveState", "User",
                                   "WorkingDirectory", "ExecStart",
                                   "MainPID"])
        if pp.get("LoadState") != "loaded" or pp.get("ActiveState") != "active":
            fail("pool listener is not loaded and active")
            continue
        if pp.get("User") != ns.docker_user:
            fail("pool listener runs under the wrong account")
            continue
        ok("pool listener is active under the docker account")
        try:
            pool_pid = int(pp.get("MainPID", "0") or "0")
        except Exception:
            pool_pid = 0
        if pool_pid <= 0:
            fail("pool listener has no MainPID; process checks unproven")
        else:
            pool_main_pids.append(pool_pid)
            pool_status = read_proc_status(pool_pid)
            if pool_status.get('Uid', '').split() != [str(dock.pw_uid)] * 4:
                fail("pool MainPID does not carry the Docker UID in all four slots")
        pdir = pp.get("WorkingDirectory", "") or ""
        if not pdir.startswith("/") or not os.path.isdir(pdir):
            fail("pool working directory is absent; boundary unproven")
            continue
        for mode in ("x", "r", "w"):
            deny_check(pdir, mode, iso.pw_uid, iso.pw_gid,
                       "isolated identity has access to a pool directory",
                       "isolated identity is denied a pool directory",
                       "pool directory access check inconclusive")
        for name in REQUIRED_SECRETS:
            sp = os.path.join(pdir, name)
            if not os.path.exists(sp):
                fail("pool registration file is absent; boundary unproven")
                continue
            deny_check(sp, "r", iso.pw_uid, iso.pw_gid,
                       "isolated identity can read a pool credential path",
                       "isolated identity cannot read pool credential paths",
                       "pool credential access check inconclusive")
            deny_check(sp, "w", iso.pw_uid, iso.pw_gid,
                       "isolated identity can write a pool credential path",
                       "isolated identity cannot write pool credential paths",
                       "pool credential write check inconclusive")
        env_path = os.path.join(pdir, ".env")
        if os.path.exists(env_path):
            deny_check(env_path, "r", iso.pw_uid, iso.pw_gid,
                       "isolated identity can read a pool env file",
                       "isolated identity cannot read pool env files",
                       "pool env access check inconclusive")
        exec_bin = parse_exec_path(pp.get("ExecStart", ""))
        if not exec_bin:
            fail("pool ExecStart shape is unreadable")
            continue
        # Walk every executable ancestor to a root-owned, non-writable
        # trust boundary: two levels miss a writable /opt or team dir that
        # still permits executable replacement below a root-owned parent.
        try:
            chain = executable_ancestors(exec_bin)
        except (OSError, ValueError):
            fail("pool executable ancestry is unreadable; boundary unproven")
            continue
        bounded = False
        for root in chain:
            if not root.startswith("/") or not os.path.exists(root):
                fail("pool executable or ancestor absent; boundary unproven")
                break
            try:
                st = os.stat(root)
            except OSError:
                fail("pool executable or ancestor unreadable; boundary unproven")
                break
            # Stat-based only: this probe runs as root, and root's
            # os.access(W_OK) answers True almost everywhere.
            if st.st_uid == 0 and not st.st_mode & 0o022:
                bounded = True
            deny_check(root, "w", iso.pw_uid, iso.pw_gid,
                       "isolated identity can write a shared pool executable or root",
                       "isolated identity cannot write shared pool executables or roots",
                       "pool executable/root write check inconclusive")
        else:
            if not bounded:
                fail("pool executable chain reaches no root-owned trust boundary")
            else:
                ok("pool executable chain reaches a root-owned trust boundary")

    if not os.path.isdir(ns.docker_home):
        fail("Docker home is absent; boundary unproven")
    else:
        for mode in ("x", "r", "w"):
            deny_check(ns.docker_home, mode, iso.pw_uid, iso.pw_gid,
                       "isolated identity has access to the docker home",
                       "isolated identity is denied the docker home",
                       "docker home access check inconclusive")

    # NOTE: no check demands the isolated account be unable to write its OWN
    # executable: the parent unit grants ReadWritePaths on the isolated tree
    # plus home for self-update.

    # --- Live docker-UID processes ---------------------------------------
    # Every live pool MainPID must be inside the UID-enumerated set: a
    # process check that misses a running pool listener proves nothing.
    pids = list_pids_of_uid(dock.pw_uid)
    if pids is None:
        fail("process inventory unreadable; Docker UID boundary unproven")
        pids = []
    missing = [p for p in pool_main_pids if p not in pids]
    if pool_main_pids and missing:
        fail("live pool MainPID(s) missing from the docker-UID process set")
    if not pids:
        fail("no docker UID processes are running; signal denial unproven")
    else:
        for pid in pids:
            code = signal0_errno_as_uid(pid, iso.pw_uid, iso.pw_gid)
            if code == errno.EPERM:
                continue
            if code == errno.ESRCH:
                fail("docker process vanished during signal check")
            else:
                fail("isolated identity is not denied signaling")
            break
        else:
            ok("isolated identity is denied signaling all docker processes")
        # /proc cmdline is world-readable by design: not checked. environ,
        # mem, and fd traversal are the private paths under test, ALL pids.
        for pid in pids:
            stop = False
            for sub in ("environ", "mem"):
                code = access_as_uid("/proc/%d/%s" % (pid, sub),
                                     "r", iso.pw_uid, iso.pw_gid)
                if code == 0:
                    fail("isolated identity can read process memory paths")
                    stop = True
                    break
                if code not in (errno.EACCES, errno.EPERM):
                    fail("process memory check inconclusive")
                    stop = True
                    break
            if stop:
                break
            code = access_as_uid("/proc/%d/fd" % pid, "x",
                                 iso.pw_uid, iso.pw_gid)
            if code == 0:
                fail("isolated identity can traverse process fd paths")
                break
            if code not in (errno.EACCES, errno.EPERM):
                fail("process fd check inconclusive")
                break
        else:
            ok("isolated identity is denied process memory and fd paths")

    # Never enter a possibly-arbitrary mount namespace on a bad identity.
    if failures:
        return finish(ns)

    # --- REAL native build + PostgreSQL smoke in the service mount ns ----
    rc = run_smoke_under_service(main_pid, iso.pw_uid, iso.pw_gid,
                                 home, ns.pg_bin_dir)
    if rc != 0:
        fail("native cc and PostgreSQL smoke failed under the service mount namespace")
    else:
        ok("native cc and PostgreSQL smoke passed under the service mount namespace")
    return finish(ns)


def finish(ns):
    if failures:
        sys.stderr.write("isolated-acceptance-probe: FAIL (%d finding(s))\n"
                         % len(failures))
        return 1
    print("isolated-acceptance-probe: PASS isolated=%s service=%s"
          % (ns.isolated_user, ns.service_name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
