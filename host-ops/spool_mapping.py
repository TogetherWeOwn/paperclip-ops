#!/usr/bin/env python3
"""Offline ID-map checks for a proposed spool carrier; never host acceptance.

Inputs must be observed maps, ordered innermost namespace to host. This helper
neither discovers namespace ancestry nor assumes that a container is nested
under `podman unshare`. A shared namespace is one layer, not an extra identity
layer. No runtime sockets, account changes, ACLs or filesystem writes occur.
"""

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import sys

MAX_ID = 2**32 - 2  # uid_t/gid_t -1 is reserved, not a mapped principal.
MAX_BYTES = 64 * 1024
MAX_EXTENTS = 340
EXPECTED_NODE_HOST_UID = 100999


class Refusal(ValueError):
    pass


def require_id(value):
    if type(value) is not int or not 0 <= value <= MAX_ID:
        raise Refusal("invalid numeric identity")
    return value


@dataclass(frozen=True)
class Extent:
    inside: int
    outside: int
    count: int


def parse_map(text):
    # Consume observations, not /etc/subuid guesses. Podman documents that
    # rootless --uidmap/--gidmap may add a second intermediate -> container map:
    # https://docs.podman.io/en/v4.9.3/markdown/podman-run.1.html#uidmap
    if not isinstance(text, str) or len(text) > MAX_BYTES:
        raise Refusal("ID map is absent or too large")
    rows = text.splitlines()
    if not 1 <= len(rows) <= MAX_EXTENTS:
        raise Refusal("unsupported ID-map extent count")
    result = []
    for row in rows:
        if not re.fullmatch(r"[ \t]*[0-9]+[ \t]+[0-9]+[ \t]+[0-9]+[ \t]*", row):
            raise Refusal("invalid ID-map row")
        inside, outside, count = map(int, row.split())
        require_id(inside)
        require_id(outside)
        if count < 1 or inside + count - 1 > MAX_ID or outside + count - 1 > MAX_ID:
            raise Refusal("invalid ID-map range")
        candidate = Extent(inside, outside, count)
        for prior in result:
            for field in ("inside", "outside"):
                a, b = getattr(candidate, field), getattr(prior, field)
                if max(a, b) < min(a + count, b + prior.count):
                    raise Refusal("overlapping ID-map ranges")
        result.append(candidate)
    return tuple(result)


def to_parent(value, extents):
    require_id(value)
    for extent in extents:
        if extent.inside <= value < extent.inside + extent.count:
            return extent.outside + value - extent.inside
    raise Refusal("identity is not mapped")


def to_host(value, maps):
    if not 1 <= len(maps) <= 8:
        raise Refusal("an explicit bounded namespace chain is required")
    for extents in maps:
        value = to_parent(value, extents)
    return value


def requester_identity(uid_maps, gid_maps, uid=1000, gid=1000, supplementary=()):
    if len(uid_maps) != len(gid_maps):
        raise Refusal("UID and GID namespace chains differ")
    host_uid = to_host(uid, uid_maps)
    groups = {to_host(value, gid_maps) for value in (gid, *supplementary)}
    if host_uid != EXPECTED_NODE_HOST_UID or 0 in groups:
        raise Refusal("requester differs from the reviewed non-root host mapping")
    return {"hostUid": host_uid, "hostGids": sorted(groups)}


def require_group_access(identity, broker_uid, requests_gid):
    require_id(broker_uid)
    require_id(requests_gid)
    if not 0 < broker_uid < 1000 or not 0 < requests_gid < 1000:
        raise Refusal("existing system broker and request-group IDs are required")
    if identity["hostUid"] == broker_uid:
        raise Refusal("requester must not become the broker owner")
    if requests_gid not in identity["hostGids"]:
        raise Refusal("mapped requester lacks the host request group; rw is not access proof")


def read_map(path):
    # Read a saved observation, not a pipe/device that can stall a preparation run.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise Refusal("ID map must be a bounded regular capture")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise Refusal("ID map is too large")
    return parse_map(raw.decode("ascii"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid-map", action="append", type=Path, required=True)
    parser.add_argument("--gid-map", action="append", type=Path, required=True)
    parser.add_argument("--supplementary-gid", action="append", type=int, default=[])
    parser.add_argument("--broker-uid", type=int, required=True)
    parser.add_argument("--requests-gid", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        identity = requester_identity([read_map(path) for path in args.uid_map],
                                       [read_map(path) for path in args.gid_map],
                                       supplementary=args.supplementary_gid)
        require_group_access(identity, args.broker_uid, args.requests_gid)
    except (Refusal, OSError, UnicodeError, ValueError):
        print("REFUSED: mapping/group contract not established", file=sys.stderr)
        return 2
    print(json.dumps({"kind": "offline-map-check", "hostAcceptance": False,
                      "identity": identity}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
