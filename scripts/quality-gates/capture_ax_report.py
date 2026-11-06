#!/usr/bin/env python3
"""Supervise one trusted AX producer; bound both pipes before returning output.

A separate process keeps the checker's subprocess.run boundary (and its fault
injection contract), without letting communicate() buffer unbounded output.
Only the pinned checker selects the producer; compared trees supply no command.
"""
import os
import selectors
import signal
import subprocess
import sys
import time

MAX_REPORT = 1024 * 1024
MAX_STDERR = 64 * 1024
TIMEOUT = 25
OUTPUT_LIMIT = 3
TIMED_OUT = 124


def capture(command, supervised=False):
    deadline = time.monotonic() + TIMEOUT
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=not supervised) as process:
        try:
            buffers = {"stdout": bytearray(), "stderr": bytearray()}
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, ("stdout", MAX_REPORT))
                selector.register(process.stderr, selectors.EVENT_READ, ("stderr", MAX_STDERR))
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return TIMED_OUT
                    for key, _ in selector.select(remaining):
                        name, limit = key.data
                        # Read only the remaining allowance plus an overflow byte.
                        chunk = os.read(key.fd, min(65536, limit - len(buffers[name]) + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                        elif len(buffers[name]) + len(chunk) > limit:
                            return OUTPUT_LIMIT
                        else:
                            buffers[name].extend(chunk)
            try:
                code = process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                return TIMED_OUT
            sys.stdout.buffer.write(buffers["stdout"])
            sys.stderr.buffer.write(buffers["stderr"])
            # Reserve supervisor error statuses; arbitrary producer errors never
            # impersonate an output-limit or timeout result.
            return 0 if code == 0 else 1
        finally:
            # Standalone capture owns its group; supervised capture shares the
            # collector's session so the outer checker also kills descendants.
            # Reap the direct child before returning either way.
            try:
                if supervised:
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


if __name__ == "__main__":
    arguments = sys.argv[1:]
    supervisor_fd = os.environ.pop("AX_COLLECTOR_SUPERVISOR_FD", None)
    supervised = supervisor_fd is not None
    if supervised:
        fd = int(supervisor_fd)
        os.write(fd, str(os.getpgrp()).encode("ascii"))
        os.close(fd)
    raise SystemExit(capture([sys.executable, "-I", "-B", *arguments], supervised=supervised))
