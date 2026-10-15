#!/usr/bin/env python3
"""Render an OFFLINE company-spool candidate from the canonical per-run carrier.

No install/activate mode exists. This does not provision paths, assign a run to a
company, validate permissions, or attest a Podman render. The privileged launcher
must establish company membership independently before selecting any such view.
"""

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "deploy/paperclip-immutable/agent-run.container.in"
# A changed carrier must be inspected before this preparation contract is updated.
# This is a source drift guard, NOT a signed-ref/host-render approval.
# Public template differs only in its first comment (private task ID removed).
TEMPLATE_SHA256 = "69fa6007f5f62a6dc9e1b3582fbf1b2f578a41e06b6b9e7a68d7f29fdab8dd8f"
MAX_TEMPLATE_BYTES = 64 * 1024
COMPANY_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
SPOOL = "/var/spool/paperclip-host-ops/companies"
DESTINATION = "/run/paperclip-host-ops"
PUBLIC_KEY = "/etc/paperclip-host-ops/response-signing.pub.pem"


class Refusal(ValueError):
    pass


@dataclass(frozen=True)
class Mount:
    source: str
    destination: str
    options: str

    def quadlet(self):
        return f"Volume={self.source}:{self.destination}:{self.options}"


def company_mounts(company_id):
    if not isinstance(company_id, str) or not COMPANY_RE.fullmatch(company_id):
        raise Refusal("company must be a canonical lowercase UUID")
    # An explicit company view is never the flat legacy spool, nor its parent.
    # No :U or :z/:Z: rendering cannot authorize ownership changes or relabeling
    # broker data shared by multiple containers. Host SELinux policy is separate.
    mounts = [Mount(f"{SPOOL}/{company_id}/{name}", f"{DESTINATION}/{name}",
                    f"{access},nosuid,nodev,noexec")
              for name, access in (("requests", "rw"), ("previews", "ro"),
                                   ("responses", "ro"))]
    mounts.append(Mount(PUBLIC_KEY, f"{DESTINATION}/response-signing.pub.pem",
                        "ro,nosuid,nodev,noexec"))
    return tuple(mounts)


def render_candidate(template, company_id):
    if not isinstance(template, bytes) or len(template) > MAX_TEMPLATE_BYTES:
        raise Refusal("canonical carrier must be bounded bytes")
    source_hash = hashlib.sha256(template).hexdigest()
    if source_hash != TEMPLATE_SHA256:
        raise Refusal("canonical per-run carrier changed; review its source first")
    mounts = company_mounts(company_id)
    text = template.decode("ascii")
    # Volume= is a repeatable [Container] key. Preserve every existing carrier
    # byte and placeholder except for inserting the explicit mount block:
    # https://docs.podman.io/en/v4.9.3/markdown/podman-systemd.unit.5.html#volume
    marker = "\n[Service]\n"
    if text.count(marker) != 1:
        raise Refusal("canonical carrier must have one Service boundary")
    block = ("# OFFLINE CANDIDATE ONLY: provisioning, routing and host evidence pending.\n"
             "# rw is not write-only proof; shared-UID requesters are not isolated.\n" +
             "\n".join(mount.quadlet() for mount in mounts) + "\n")
    candidate = text.replace(marker, "\n" + block + marker)
    return {
        "kind": "offline-company-spool-carrier",
        "schemaVersion": 1,
        "companyId": company_id,
        "hostAcceptance": False,
        "activationReady": False,
        "writeOnlyIngressProven": False,
        "companyIsolationProven": False,
        "canonicalTemplateSha256": source_hash,
        "candidateSha256": hashlib.sha256(candidate.encode("ascii")).hexdigest(),
        "mounts": [asdict(mount) for mount in mounts],
        "pending": ["trusted-launcher-company-binding", "broker-company-routing",
                    "journaled-provisioning", "observed-uid-gid-mapping",
                    "requester-dac-probes", "host-generator-render",
                    "exact-head-review-and-merge"],
        "candidateQuadlet": candidate,
    }


def read_template(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TEMPLATE_BYTES:
            raise Refusal("canonical carrier must be a bounded regular file")
        content = stream.read(MAX_TEMPLATE_BYTES + 1)
    if len(content) > MAX_TEMPLATE_BYTES:
        raise Refusal("canonical carrier is too large")
    return content


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company-id", required=True)
    args = parser.parse_args(argv)
    try:
        result = render_candidate(read_template(TEMPLATE), args.company_id)
    except (Refusal, OSError, UnicodeError):
        print("REFUSED: offline carrier source or company contract failed", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
