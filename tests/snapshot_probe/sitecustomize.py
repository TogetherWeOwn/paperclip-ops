"""Test-only read-size observer; leaves the wrapper's read behavior unchanged.

Activated only for the inline Python wrapper process, never the cp test double.
The fixed probe input is at most 65,536 bytes, not a resource-exhaustion test.
"""
import builtins
import io
import json
import os
from pathlib import Path
import sys


if os.environ.get('SNAPSHOT_READ_PROBE') == '1' and sys.argv[0] == '-':
    _open = builtins.open
    _io_open = io.open
    _root = Path(os.environ['OPREQ_SANDBOX']).resolve(strict=True)
    _trace = _root / 'read-observations.jsonl'

    def record(event):
        with _open(_trace, 'a') as stream:
            stream.write(json.dumps(event) + '\n')

    record({'event': 'active', 'pid': os.getpid()})

    class ObservedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            record({'event': 'read', 'requested': size})
            data = self.stream.read(size)
            record({'event': 'returned', 'bytes': len(data)})
            return data

        def read1(self, size=-1):
            record({'event': 'read', 'requested': size})
            data = self.stream.read1(size)
            record({'event': 'returned', 'bytes': len(data)})
            return data

        def readall(self):
            record({'event': 'read', 'requested': -1})
            data = self.stream.readall()
            record({'event': 'returned', 'bytes': len(data)})
            return data

    def wrap(opener, file, *args, **kwargs):
        stream = opener(file, *args, **kwargs)
        mode = args[0] if args else kwargs.get('mode', 'r')
        if isinstance(file, (str, bytes, os.PathLike)) and 'r' in mode:
            path = Path(os.fsdecode(file))
            if path.parent == _root / 'queue':
                return ObservedReader(stream)
        return stream

    def observed_open(file, *args, **kwargs):
        return wrap(_open, file, *args, **kwargs)

    def observed_io_open(file, *args, **kwargs):
        return wrap(_io_open, file, *args, **kwargs)

    builtins.open = observed_open
    io.open = observed_io_open
