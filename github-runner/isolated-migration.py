#!/usr/bin/env python3
"""Offline, registration-preserving handoff. Never stops or starts a service.

Host-only entrypoint; tests exercise filesystem mechanics in temporary directories.
No GitHub API, registration token, config.sh, install.sh or remove.sh invocation.
"""
import argparse
import contextlib
import fcntl
import hashlib
import grp
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = Path('/opt/two-isolated')
STATE = Path('/var/lib/two-isolated-migration')
SYSTEMD = Path('/etc/systemd/system')
PROC = Path('/proc')
CGROUP = Path('/sys/fs/cgroup')
REGISTRATION = ('.runner', '.credentials', '.credentials_rsaparams')
PRESERVED = REGISTRATION + ('.env', '.path', '.service')

# Resource-gate thresholds (single place; values documented in
# docs/isolated-runner-migration.md, "Idle-only migration" preflight).
# case_35135887 (case_be10e9e2 step 4): host load average is deliberately NOT a gate.
# It counts runnable tasks regardless of priority, so on the retired box
# (prod/staging only) it says nothing about this migration's target — which
# is stopped at preflight and therefore has no pressure signal to read
# either. Target health is proven by stopped()/idle_processes()/registration
# digests; host load remains evidence-only in verify-fleet.sh's headroom
# record.
DISK_FREE_MIN_BYTES = 5 * 1024**3
INODES_FREE_MIN = 10000


class Refused(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def command(*argv):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, timeout=30,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C'})
    require(result.returncode == 0, 'required host command failed (output withheld)')
    return result.stdout.strip()


def properties(service):
    keys = ('ActiveState', 'SubState', 'MainPID', 'ControlPID', 'ControlGroup',
            'User', 'Group', 'WorkingDirectory', 'FragmentPath', 'DropInPaths',
            'EnvironmentFiles', 'LoadState', 'ExecStart', 'InaccessiblePaths',
            'NoNewPrivileges', 'CapabilityBoundingSet', 'AmbientCapabilities',
            'ProtectSystem', 'ProtectHome', 'PrivateTmp', 'ProtectProc',
            'RestrictSUIDSGID', 'RestrictNamespaces', 'LockPersonality',
            'RestrictRealtime', 'UMask', 'SupplementaryGroups', 'ReadWritePaths', 'Environment')
    raw = command('systemctl', 'show', service, *['--property=' + k for k in keys])
    result = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    require(all(k in result for k in keys), 'incomplete systemd response')
    require(result['LoadState'] == 'loaded', 'service is not loaded')
    return result


def stopped(service):
    p = properties(service)
    require(p['ActiveState'] == 'inactive' and p['SubState'] == 'dead'
            and p['MainPID'] == '0' and p['ControlPID'] == '0',
            'listener must already be stopped, inactive/dead, with no control process')
    if p['ControlGroup']:
        cg = Path('/sys/fs/cgroup') / p['ControlGroup'].lstrip('/')
        require('..' not in cg.parts, 'invalid cgroup path')
        if cg.exists():
            for file in cg.rglob('cgroup.procs'):
                require(not file.read_text().strip(), 'listener cgroup still has processes')
    return p


def safe_path(path):
    require(path.is_absolute() and re.fullmatch(r'/[A-Za-z0-9_./-]+', str(path))
            and '..' not in path.parts, 'path is not a safe absolute pathname')
    require(path.resolve() == path, 'path or an ancestor is a symlink')


def protected_parent(path):
    safe_path(path)
    for parent in (path, *path.parents):
        s = parent.stat()
        require(s.st_uid == 0 and not s.st_mode & 0o022,
                'destination/state ancestry must be root-owned and not group/world writable')


def inventory(tree):
    """Do not follow symlinks or cross mounts; retain exact rollback metadata."""
    device = tree.lstat().st_dev
    records = []
    paths = [tree]
    for directory, dirs, files in os.walk(tree, followlinks=False):
        paths.extend(Path(directory) / n for n in dirs + files)
    for path in sorted(paths, key=lambda p: str(p.relative_to(tree))):
        s = path.lstat()
        require(s.st_dev == device, 'runner tree crosses a filesystem boundary')
        require(stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode) or stat.S_ISLNK(s.st_mode),
                'runner tree contains a socket/device/FIFO; inspect offline')
        require(not stat.S_ISREG(s.st_mode) or s.st_nlink == 1,
                'runner tree contains a hardlinked file; inspect offline')
        require(not s.st_mode & 0o6000, 'runner tree contains set-ID bits')
        attrs = os.listxattr(path, follow_symlinks=False)
        require(not any(a in attrs for a in ('security.capability', 'system.posix_acl_access',
                                             'system.posix_acl_default')),
                'runner tree contains capabilities or ACLs; inspect offline')
        records.append({'path': str(path.relative_to(tree)), 'uid': s.st_uid, 'gid': s.st_gid,
                        'mode': stat.S_IMODE(s.st_mode), 'ino': s.st_ino, 'type': stat.S_IFMT(s.st_mode)})
    return records


def no_mounts(tree):
    # st_dev alone misses same-filesystem bind mounts. Refuse descendants too.
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        mount = line.split()[4]
        mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), mount)
        require(not (mount == str(tree) or mount.startswith(str(tree) + '/')),
                'runner tree is or contains a mountpoint')


def no_open_handles(tree, uid):
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            status = (proc / 'status').read_text()
            fields = dict(line.split(':', 1) for line in status.splitlines() if ':' in line)
            if uid not in [int(x) for x in fields['Uid'].split()]:
                continue
            for link in [proc / 'cwd', proc / 'root', proc / 'exe', *(proc / 'fd').iterdir()]:
                try:
                    target = os.readlink(link).removesuffix(' (deleted)')
                except FileNotFoundError:
                    continue
                require(not (target == str(tree) or target.startswith(str(tree) + '/')),
                        'Docker-UID process retains a runner-tree handle; leave stopped')
            maps = (proc / 'maps').read_text()
            require(not any(str(tree) + '/' in line for line in maps.splitlines()),
                    'Docker-UID process retains a runner-tree mapping; leave stopped')
        except FileNotFoundError:
            continue


def digests(tree):
    result = {}
    for name in PRESERVED:
        path = tree / name
        if name in REGISTRATION:
            require(path.exists(), 'required registration file absent')
        if path.exists():
            s = path.lstat()
            require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1, 'registration/env file is not a private regular file')
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    require(json.loads((tree / '.runner').read_text()).get('workFolder') == '_work',
            'runner workFolder must be relative _work; inspect custom configuration')
    return result


def fsync_dir(directory):
    """Persist directory entries after renames; a crash must not lose the journal."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_file(path):
    with path.open('rb') as handle:
        os.fsync(handle.fileno())


def write_state(directory, data):
    temp = directory / 'state.new'
    with temp.open('w') as handle:
        json.dump(data, handle)
        handle.flush()
        os.fchmod(handle.fileno(), 0o600)
        os.fsync(handle.fileno())
    temp.replace(directory / 'state.json')
    fsync_dir(directory)


def change_owners(tree, records, old_uid, old_gid, new_uid, new_gid):
    for record in records:
        path = tree / record['path']
        os.chown(path, new_uid if record['uid'] == old_uid else record['uid'],
                 new_gid if record['gid'] == old_gid else record['gid'], follow_symlinks=False)
    # Credentials are private, but their bytes (and registration ID) do not change.
    for name in PRESERVED:
        if (tree / name).exists():
            os.chown(tree / name, new_uid, new_gid)
            os.chmod(tree / name, 0o600)
    os.chown(tree, new_uid, new_gid)
    os.chmod(tree, 0o700)


def restore_owners(tree, data):
    original = {r['path']: r for r in data['inventory']}
    for r in inventory(tree):
        path = tree / r['path']
        before = original.get(r['path'])
        if before and before['ino'] == r['ino'] and before['type'] == r['type']:
            os.chown(path, before['uid'], before['gid'], follow_symlinks=False)
            if r['type'] != stat.S_IFLNK:
                os.chmod(path, before['mode'])
        else:
            require(r['uid'] in (0, data['new_uid'], data['old_uid']), 'unexpected rollback file owner')
            os.chown(path, data['old_uid'] if r['uid'] == data['new_uid'] else r['uid'],
                     data['old_gid'] if r['gid'] == data['new_gid'] else r['gid'], follow_symlinks=False)


def accounts(name):
    old = pwd.getpwnam('gha-runner')
    new = pwd.getpwnam('gha-isolated-' + name[-1])
    require(old.pw_uid != new.pw_uid and old.pw_uid != 0 and new.pw_uid != 0, 'UIDs must differ and be non-root')
    require(sum(p.pw_uid == new.pw_uid for p in pwd.getpwall()) == 1, 'isolated UID has another login name')
    require(set(os.getgrouplist(new.pw_name, new.pw_gid)) == {new.pw_gid}, 'isolated account has supplementary groups')
    require(new.pw_gid != old.pw_gid and new.pw_gid != 0, 'isolated group is shared with Docker/root')
    require(grp.getgrnam(new.pw_name).gr_gid == new.pw_gid
            and sum(p.pw_gid == new.pw_gid for p in pwd.getpwall()) == 1,
            'isolated primary group must be private and named for its account')
    require(new.pw_dir == '/var/lib/' + new.pw_name and new.pw_shell in ('/usr/sbin/nologin', '/sbin/nologin'),
            'isolated home/shell differs from the runbook')
    lock = command('passwd', '-S', new.pw_name).split()
    require(len(lock) > 1 and lock[1] == 'L', 'isolated password must be locked')
    for path in (Path(old.pw_dir), Path(new.pw_dir)):
        safe_path(path)
        s = path.stat()
        require(stat.S_IMODE(s.st_mode) == 0o700, 'account home must be mode 0700 (no live chmod by this helper)')
        expected = old.pw_uid if path == Path(old.pw_dir) else new.pw_uid
        require(s.st_uid == expected, 'account home ownership mismatch')
        require(not any(a.startswith('system.posix_acl') for a in os.listxattr(path)), 'home has an ACL')
    require(not Path('/run/user/' + str(new.pw_uid)).exists(), 'isolated account must have no user manager/runtime (no linger)')
    # An allowed limited sudo command also fails this check; do not test only sudo true.
    sudo = subprocess.run(['sudo', '-n', '-l', '-U', new.pw_name], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C'}, timeout=30)
    require(sudo.returncode == 1 and b'not allowed to run sudo' in sudo.stderr + sudo.stdout,
            'cannot prove absence of all sudo grants')
    return old, new


def check_resources(source, destination_parent):
    require(source.stat().st_dev == destination_parent.stat().st_dev,
            'source and destination must share a filesystem for atomic rename')
    require(shutil.disk_usage(source).free >= DISK_FREE_MIN_BYTES, 'less than 5 GiB free')
    vfs = os.statvfs(source)
    require(vfs.f_favail >= INODES_FREE_MIN, 'fewer than 10000 free inodes')


def preflight(name, source):
    old, new = accounts(name)
    service = 'actions.runner.TogetherWeOwn.' + name + '.service'
    p = stopped(service)
    require(p['User'] == old.pw_name and p['Group'] in ('', old.pw_name), 'unexpected old service identity')
    require(not p['DropInPaths'] and not p['EnvironmentFiles'], 'custom unit overrides/environment files require review')
    unit = SYSTEMD / service
    require(p['FragmentPath'] == str(unit), 'unit is not a local system unit')
    require(unit.is_file() and not unit.is_symlink() and unit.stat().st_uid == 0
            and not unit.stat().st_mode & 0o022, 'unit is not a protected root-owned regular file')
    safe_path(source)
    require(source.parent == Path(old.pw_dir) and source.name not in ('', '.', '..'),
            'source must be one direct runner directory inside the Docker account home')
    require(p['WorkingDirectory'] == str(source), 'source differs from effective service working directory')
    require((source / 'runsvc.sh').is_file() and not (source / 'runsvc.sh').is_symlink(), 'runsvc.sh missing or symlinked')
    for path in (ROOT, STATE):
        protected_parent(path)
    require(stat.S_IMODE(STATE.stat().st_mode) == 0o700, 'state root must be mode 0700')
    check_resources(source, ROOT)
    require(source.stat().st_dev == STATE.stat().st_dev, 'private staging must share the runner filesystem')
    no_mounts(source)
    no_open_handles(source, old.pw_uid)
    records = inventory(source)
    require(all(r['uid'] in (0, old.pw_uid) and r['gid'] in (0, old.pw_gid) for r in records),
            'unexpected tree owner/group; inspect offline, do not recursively normalize')
    for name_ in ('.env', '.path'):
        path = source / name_
        if path.exists():
            content = path.read_text()
            require(not re.search(r'^(DOCKER_HOST|CONTAINER_HOST|XDG_RUNTIME_DIR)=', content, re.M)
                    and old.pw_dir not in content, 'runner env/path retains a Docker account dependency')
    fingerprints = digests(source)
    config = json.loads((source / '.runner').read_text())
    require(config.get('agentName') == name, 'runner registration name differs from target')
    return {'version': 1, 'phase': 'planned', 'source': str(source), 'target': str(ROOT / name),
            'service': service, 'old_uid': old.pw_uid, 'old_gid': old.pw_gid,
            'new_uid': new.pw_uid, 'new_gid': new.pw_gid, 'new_user': new.pw_name,
            'home': new.pw_dir, 'digests': fingerprints, 'inventory': records,
            'unit_mode': stat.S_IMODE(unit.stat().st_mode)}


def stage(data, directory):
    source, target = Path(data['source']), Path(data['target'])
    unit = SYSTEMD / data['service']
    private_tree = directory / 'tree'
    require(not target.exists() and not private_tree.exists(), 'destination/staging already exists')
    # Journal before the first mutation, with private metadata/old unit, never credentials in output.
    shutil.copyfile(unit, directory / 'unit.before')
    os.chmod(directory / 'unit.before', 0o600)
    fsync_file(directory / 'unit.before')
    fsync_dir(directory)
    write_state(directory, data)
    source.rename(private_tree)
    fsync_dir(source.parent)
    fsync_dir(directory)
    no_open_handles(private_tree, data['old_uid'])
    require(inventory(private_tree) == data['inventory'], 'tree changed during handoff; rollback while stopped')
    require(digests(private_tree) == data['digests'], 'registration changed during handoff')
    change_owners(private_tree, data['inventory'], data['old_uid'], data['old_gid'], data['new_uid'], data['new_gid'])
    private_tree.rename(target)
    fsync_dir(directory)
    fsync_dir(target.parent)
    template = (HERE / 'actions-runner-isolated.service.in').read_text()
    for key, value in {'RUNNER_USER': data['new_user'], 'RUNNER_HOME': str(target), 'USER_HOME': data['home']}.items():
        template = template.replace('@' + key + '@', value)
    require('@' not in template, 'unresolved unit placeholder')
    candidate = directory / data['service']
    candidate.write_text(template)
    fsync_file(candidate)
    fsync_dir(directory)
    command('systemd-analyze', 'verify', str(candidate))
    # Atomic install into the protected unit directory; never start or stop here.
    temp = SYSTEMD / (data['service'] + '.new')
    require(not temp.exists() and not temp.is_symlink(), 'unit temporary path already exists')
    with temp.open('x') as handle:
        handle.write(template)
        handle.flush()
        os.fchmod(handle.fileno(), 0o644)
        os.fsync(handle.fileno())
    temp.replace(unit)
    fsync_dir(SYSTEMD)
    command('systemctl', 'daemon-reload')
    require(digests(target) == data['digests'], 'registration changed; leave listener stopped')
    data['phase'] = 'prepared'
    write_state(directory, data)


def rollback(data, directory):
    stopped(data['service'])
    source, target, private_tree = Path(data['source']), Path(data['target']), directory / 'tree'
    present = [p for p in (source, target, private_tree) if p.exists()]
    require(len(present) == 1, 'ambiguous recovery paths; leave stopped and inspect private journal')
    tree = present[0]
    no_mounts(tree)
    no_open_handles(tree, data['new_uid'])
    no_open_handles(tree, data['old_uid'])
    require(digests(tree) == data['digests'], 'registration changed since snapshot; no automatic rollback')
    if tree != private_tree:
        tree.rename(private_tree)
        fsync_dir(tree.parent)
        fsync_dir(directory)
    restore_owners(private_tree, data)
    require(not source.exists(), 'original source path was recreated; no overwrite')
    private_tree.rename(source)
    fsync_dir(directory)
    fsync_dir(source.parent)
    unit = SYSTEMD / data['service']
    backup = directory / 'unit.before'
    temp = SYSTEMD / (data['service'] + '.rollback')
    require(not temp.exists() and not temp.is_symlink(), 'rollback temporary path already exists')
    with temp.open('xb') as handle:
        handle.write(backup.read_bytes())
        handle.flush()
        os.fchmod(handle.fileno(), data['unit_mode'])
        os.fsync(handle.fileno())
    temp.replace(unit)
    fsync_dir(SYSTEMD)
    command('systemctl', 'daemon-reload')
    data['phase'] = 'rolled_back'
    write_state(directory, data)


@contextlib.contextmanager
def locked():
    protected_parent(STATE)
    fd = os.open(STATE / 'migration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        require(os.fstat(handle.fileno()).st_uid == 0, 'migration lock not root-owned')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('another listener handoff is running')
        yield


def idle_processes(data, props):
    """Require the three-process runsvc -> node -> Listener service, no jobs.

    Sources: actions/runner src/Misc/layoutbin/{runsvc.sh,RunnerService.js}.
    An idle service is NOT just MainPID: runsvc waits on node, which waits on
    Runner.Listener. Refuse workers, extra descendants and unknown layouts.
    Bind start times as well as PIDs so restarts/PID reuse invalidate acceptance.
    """
    require(props['ActiveState'] == 'active' and props['SubState'] == 'running'
            and props['ControlPID'] == '0', 'service is not active/idle')
    group = props['ControlGroup']
    require(group.startswith('/') and group != '/' and '..' not in Path(group).parts,
            'service cgroup is missing or invalid')
    cg = CGROUP / group.lstrip('/')
    require((cg / 'cgroup.procs').is_file(), 'service cgroup is unreadable')
    def unreadable(error):
        raise error

    pids = set()
    for directory, dirs, files in os.walk(cg, onerror=unreadable):
        require('cgroup.procs' in files, 'descendant cgroup is unreadable')
        pids.update(int(n) for n in (Path(directory) / 'cgroup.procs').read_text().split())
    main_pid = int(props['MainPID'])
    require(main_pid > 0 and main_pid in pids and len(pids) == 3,
            'service holds job processes or lacks the idle listener chain')
    tree = Path(data['target'])
    roles, snapshot = {}, set()
    for pid in pids:
        proc = PROC / str(pid)
        fields = dict(line.split(':', 1) for line in (proc / 'status').read_text().splitlines() if ':' in line)
        require(fields['Uid'].split() == [str(data['new_uid'])] * 4,
                'listener chain UID changed')
        argv = (proc / 'cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
        exe = Path(os.readlink(proc / 'exe'))
        require(Path(os.readlink(proc / 'cwd')) == tree, 'listener chain cwd differs')
        if pid == main_pid:
            require(exe in (Path('/usr/bin/bash'), Path('/bin/bash'))
                    and len(argv) == 2 and Path(argv[1]) == tree / 'runsvc.sh',
                    'MainPID is not the idle runsvc wrapper')
            role = 'wrapper'
        elif exe == tree / 'bin/Runner.Listener':
            require(argv == [str(exe), 'run', '--startuptype', 'service'],
                    'listener argv differs from service mode')
            role = 'listener'
        else:
            require(re.fullmatch(re.escape(str(tree)) + r'/externals/node[0-9]+/bin/node', str(exe))
                    and len(argv) == 2 and argv[1] == './bin/RunnerService.js',
                    'unexpected process in listener cgroup; queue is not idle')
            role = 'node'
        require(role not in roles, 'duplicate listener-chain process')
        roles[role] = (pid, int(fields['PPid']))
        # /proc/PID/stat field 22; comm can contain spaces and parentheses.
        start = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
        snapshot.add((pid, start))
    require(set(roles) == {'wrapper', 'node', 'listener'}
            and roles['node'][1] == main_pid and roles['listener'][1] == roles['node'][0],
            'listener parent chain differs')
    return snapshot


def accept(data, directory, args):
    """Require held admission and revalidate idle identity after the long probe.

    Do not compare the offline inventory: ownership intentionally changed and
    the live listener writes diagnostics even while idle. Instead bind the
    idle process chain, registration bytes, effective unit and journaled bytes.
    """
    require(args.queue_held_idle_confirmed, 'accept requires held queue and idle attestation')
    require(data['phase'] == 'prepared', 'accept requires prepared journal')
    require(digests(Path(data['target'])) == data['digests'], 'registration changed before acceptance')
    candidate = directory / data['service']
    unit_bytes = candidate.read_bytes()
    require((SYSTEMD / data['service']).read_bytes() == unit_bytes,
            'live unit differs from journaled candidate')
    before = properties(data['service'])
    processes = idle_processes(data, before)
    no_open_handles(Path(data['target']), data['old_uid'])
    unit_sha256 = hashlib.sha256(unit_bytes).hexdigest()
    argv = ['bash', str(HERE / 'isolated-acceptance-probe.sh'), '--service-name', data['service'],
            '--isolated-user', data['new_user'], '--docker-user', 'gha-runner',
            '--docker-home', pwd.getpwnam('gha-runner').pw_dir, '--pg-bin-dir', args.pg_bin_dir,
            '--unit-sha256', unit_sha256]
    for n in range(1, 6):
        argv += ['--pool-service', 'actions.runner.TogetherWeOwn.coolify-vps-' + str(n) + '.service']
    # The probe bounds each subprocess itself. An outer kill can
    # interrupt its PostgreSQL finally block and strand smoke data.
    result = subprocess.run(argv, check=False)
    require(result.returncode == 0, 'acceptance failed; do not migrate sibling or resume queue')
    after = properties(data['service'])
    require(after == before, 'effective service changed during acceptance probe')
    require(idle_processes(data, after) == processes,
            'listener chain changed during acceptance probe')
    no_open_handles(Path(data['target']), data['old_uid'])
    require(digests(Path(data['target'])) == data['digests'],
            'registration changed during acceptance probe')
    require((SYSTEMD / data['service']).read_bytes() == candidate.read_bytes() == unit_bytes,
            'unit changed during acceptance probe')
    data['phase'] = 'accepted'
    write_state(directory, data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['preflight', 'apply', 'rollback', 'accept'])
    parser.add_argument('--runner-name', required=True, choices=['coolify-vps-6', 'coolify-vps-7'])
    parser.add_argument('--source', type=Path)
    parser.add_argument('--queue-held-idle-confirmed', action='store_true')
    parser.add_argument('--pg-bin-dir', default='/usr/lib/postgresql/16/bin')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'host operator must run as root; never run via CI')
    os.umask(0o077)
    directory = STATE / args.runner_name
    protected_parent(STATE)
    require(stat.S_IMODE(STATE.stat().st_mode) == 0o700, 'state root must be mode 0700')
    if args.action == 'preflight':
        require(args.source is not None, '--source required')
        preflight(args.runner_name, args.source)
        print('PASS: offline preflight; no changes made')
        return
    with locked():
        if args.action == 'apply':
            require(args.queue_held_idle_confirmed, 'hold queue admission and confirm idle before stopping')
            if (directory / 'state.json').exists():
                data = json.loads((directory / 'state.json').read_text())
                require(data['phase'] == 'prepared', 'journal exists; inspect or rollback, never overwrite')
                stopped(data['service'])
                require(digests(Path(data['target'])) == data['digests'], 'prepared registration differs')
                require((SYSTEMD / data['service']).read_bytes() == (directory / data['service']).read_bytes(),
                        'prepared unit differs')
                print('PASS: already prepared, still stopped; no changes made')
                return
            require(args.source is not None, '--source required')
            for journal in STATE.glob('*/state.json'):
                require(json.loads(journal.read_text())['phase'] in ('accepted', 'rolled_back'),
                        'other listener is not accepted/rolled back; finish it first')
            require(not directory.exists(), 'unfinished journal directory; inspect rather than overwrite')
            data = preflight(args.runner_name, args.source)
            directory.mkdir(mode=0o700)
            fsync_dir(STATE)
            stage(data, directory)
            print('PASS: prepared with original registration; listener remains stopped; acceptance required')
        else:
            protected_parent(directory)
            data = json.loads((directory / 'state.json').read_text())
            if args.action == 'rollback':
                require(args.queue_held_idle_confirmed, 'rollback requires held queue and idle/stopped service')
                rollback(data, directory)
                print('PASS: previous bytes/ownership restored; listener remains STOPPED on insecure legacy identity')
            else:
                accept(data, directory, args)
                print('PASS: host probe recorded for this listener only; GitHub ID/labels and bounded job still require operator evidence')


if __name__ == '__main__':
    try:
        main()
    except (Refused, OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
        # Do not stringify arbitrary input/command errors: they may contain credential bytes.
        detail = str(error) if isinstance(error, Refused) else type(error).__name__ + ' (details withheld)'
        print('REFUSED: ' + detail + '; no automatic start/stop, inspect journal if apply began', file=sys.stderr)
        sys.exit(2)
