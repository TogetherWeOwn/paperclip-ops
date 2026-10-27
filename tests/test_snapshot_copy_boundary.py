#!/usr/bin/python3
"""Test-only cp boundary: mutate the original inode, then copy real bytes.

Only invoked by test_op_request_snapshot_acceptance.py in private scratch.
The production wrapper is not patched at the validation/publication boundary.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    root = Path(os.environ['OPREQ_SANDBOX']).resolve(strict=True)
    if os.geteuid() == 0 or root.stat().st_uid != os.geteuid():
        raise RuntimeError('test boundary refuses root or foreign scratch')
    args = sys.argv[1:]
    if len(args) != 3 or args[0] != '--':
        raise RuntimeError('unexpected cp arguments')
    src, dest = Path(args[1]), Path(args[2])
    if dest.parent != root / 'queue' or dest.is_symlink() or dest.exists():
        raise RuntimeError('unexpected destination')
    resolved = str(src.resolve(strict=True))
    if not (resolved.startswith(str(root) + '/') or resolved.startswith('/memfd:')):
        raise RuntimeError('copy source is not scratch or an inherited memfd')
    original = root / 'src.json'
    before = original.stat()
    before_bytes = original.read_bytes()
    mode = os.environ['SNAPSHOT_MUTATION']
    if mode == 'same-inode':
        replacement = b'{"what":"UNVALIDATEDxx","why":"test only","rollback":"discard dummy"}\n'
    elif mode == 'growth':
        replacement = b'x' * 65536
    elif mode == 'none':
        replacement = None
    else:
        raise RuntimeError('unknown mutation')
    if replacement is not None:
        # r+b truncates/writes the existing inode: no rename, unlink or replace.
        with original.open('r+b') as stream:
            stream.write(replacement)
            stream.truncate()
            stream.flush()
    after = original.stat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise RuntimeError('probe replaced the inode')
    after_bytes = original.read_bytes()
    if mode != 'none' and after_bytes == before_bytes:
        raise RuntimeError('mutation did not change the original bytes')
    # Consume the actual inherited fd/path supplied by production, not a copy
    # of the fixture or a recomputed expected value.
    shutil.copyfile(src, dest)
    data = dest.read_bytes()
    if len(data) > 65536:
        raise RuntimeError('probe exceeded its fixed scratch-size ceiling')
    with (root / 'copy-observation.json').open('x') as stream:
        json.dump({'mutation': mode, 'same_inode': True,
                   'source_before_size': before.st_size,
                   'source_after_size': after.st_size,
                   'source_before_sha256': sha(before_bytes),
                   'source_after_sha256': sha(after_bytes),
                   'source_identity_before': [before.st_dev, before.st_ino],
                   'source_identity_after': [after.st_dev, after.st_ino],
                   'copied_size': len(data), 'copied_sha256': sha(data)}, stream)


if __name__ == '__main__':
    main()
