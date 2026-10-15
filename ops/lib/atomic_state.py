"""Atomic, fail-closed JSON state for the ops guards.

save() writes a temp file beside the target, fsyncs it and renames it over the target, so an ENOSPC or an
interrupted write leaves the previous state in place. load() raises StateError for missing, empty, malformed or
invalid state and callers stop before any side effect. recover() is the only path that replaces state, and only
when the current state is missing, corrupt, or explicitly allowed by may_replace; it keeps the replaced bytes and
appends a provenance line to state-recovery.jsonl beside the state file.
"""
import contextlib
import datetime as dt
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path


class StateError(RuntimeError):
    pass


def load(path, validate):
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise StateError(f'{path} is missing')
    try:
        data = json.loads(raw)
        validate(data)
    except ValueError as e:
        raise StateError(f'{path} is corrupt: {e}')
    return data


def _write_bytes_atomic(path, payload):
    """Core: same-directory temp file, fsync, atomic replace, temp cleanup."""
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write_json_atomic(path, data):
    """Write JSON via a same-directory temp file and atomic replace."""
    path = Path(path)
    payload = (json.dumps(data, indent=1, sort_keys=True) + '\n').encode()
    _write_bytes_atomic(path, payload)


def write_text_atomic(path, text, encoding='utf-8'):
    """Write text via a same-directory temp file and atomic replace."""
    path = Path(path)
    payload = text.encode(encoding) if isinstance(text, str) else bytes(text)
    _write_bytes_atomic(path, payload)


def save(path, data, validate):
    path = Path(path)
    validate(data)
    write_json_atomic(path, data)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def recover(path, data, validate, provenance, may_replace=lambda _old: False):
    path = Path(path)
    validate(data)
    try:
        old = load(path, validate)
    except StateError:
        old = None
    if old is not None and not may_replace(old):
        raise StateError(f'{path} is valid; recovery refuses to replace it')
    now = dt.datetime.now(dt.timezone.utc)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.replaced-{now.strftime('%Y%m%dT%H%M%SZ')}")
        shutil.copy2(path, backup)
    entry = {'at': now.isoformat(), 'path': str(path), 'backup': str(backup) if backup else None,
             'provenance': provenance, 'state': data}
    with (path.parent / 'state-recovery.jsonl').open('a') as f:
        f.write(json.dumps(entry, sort_keys=True) + '\n')
        f.flush()
        os.fsync(f.fileno())
    save(path, data, validate)
