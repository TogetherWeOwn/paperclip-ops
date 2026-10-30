#!/usr/bin/env python3
"""Fail-closed filesystem boundary for the graded CLI and all its descendants.

Only system executables/libraries and the fresh private runtime are visible.
The controller builds inline input outside this process; no grader, repository
worktree, Git metadata, or other-arm output directory is granted here. Landlock
needs no root, user namespace, host installation or persistent configuration.
"""
from __future__ import annotations

import ctypes
import errno
import os
import platform
import sys

# Linux UAPI, include/uapi/linux/landlock.h. Require ABI 3 so truncation and
# cross-directory rename/link are mediated as well as ordinary reads/writes.
_CREATE, _ADD, _RESTRICT = 444, 445, 446
_READ = (1 << 0) | (1 << 2) | (1 << 3)  # execute, read file, read directory
_ALL = (1 << 15) - 1
_WRITE = _ALL & ~((1 << 6) | (1 << 11))  # never create character/block devices


class _Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def confine(runtime: str) -> None:
    """Irreversibly confine THIS process. On unavailable enforcement, raise OSError."""
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "aarch64"):
        raise OSError(errno.ENOSYS, "review isolation requires Linux Landlock ABI >=3")
    runtime = os.path.realpath(runtime)
    if not os.path.isdir(runtime) or runtime == "/" or any(
            runtime == root or runtime.startswith(root + "/")
            for root in ("/usr", "/bin", "/lib", "/lib64")):
        raise OSError(errno.EINVAL, "runtime must be a private non-system directory")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    def syscall(number, *args):
        result = libc.syscall(ctypes.c_long(number), *args)
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, f"Landlock enforcement unavailable: {os.strerror(error)}")
        return result

    if syscall(_CREATE, None, ctypes.c_size_t(0), ctypes.c_uint(1)) < 3:
        raise OSError(errno.ENOSYS, "review isolation requires Landlock ABI >=3")
    attr = _Ruleset(_ALL)
    ruleset = syscall(_CREATE, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    try:
        # Follow symlinks only while granting the trusted runtime roots. Later
        # symlinks from the writable runtime cannot grant access to other trees.
        paths = [(root, _READ) for root in ("/usr", "/bin", "/lib", "/lib64")
                 if os.path.exists(root)]
        # Node/OpenSSL startup, TLS verification and DNS need these public
        # system files. Never grant /etc as a whole (configs may hold secrets).
        paths += [(path, (1 << 2) | (1 << 3) if os.path.isdir(path) else (1 << 2))
                  for path in ("/etc/ssl/openssl.cnf", "/etc/ssl/certs", "/etc/hosts",
                               "/etc/resolv.conf", "/etc/nsswitch.conf") if os.path.exists(path)]
        paths.append((runtime, _WRITE))
        for path, rights in paths:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = _PathBeneath(rights, fd)
                syscall(_ADD, ruleset, 1, ctypes.byref(rule), 0)
            finally:
                os.close(fd)
        # No privilege escalation can escape this policy, including on exec.
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            error = ctypes.get_errno()
            raise OSError(error, "cannot set no_new_privs for review isolation")
        syscall(_RESTRICT, ruleset, 0)
    finally:
        os.close(ruleset)


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--":
        print("usage: isolation.py PRIVATE_RUNTIME -- COMMAND [ARGS...]", file=sys.stderr)
        return 2
    try:
        runtime = os.path.realpath(argv[0])
        os.chdir(runtime)
        confine(runtime)
        os.execvpe(argv[2], argv[2:], os.environ)
    except OSError as exc:
        print(f"review isolation refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
