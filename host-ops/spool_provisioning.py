#!/usr/bin/env python3
"""Offline company-spool provisioning contract; never host provisioning.

Renders an offline tmpfiles candidate for the per-company spool layout and
validates a proposed provisioned tree on a fixture root. This does not create
directories, assign a run to a company, change ownership, or attest a host
render. The privileged provisioning step, exact operator commands and host
evidence remain outstanding in the parent-owned host window.

Validation mirrors the reviewed broker contract it must agree with:
``validate_company_spools`` in ``host-ops/broker.py`` on the sibling
``case_9011453f-write-executors`` branch (PR #393, case_94e67145, merged there, not yet
on main). The broker is authoritative: a company the broker refuses must never
be provisioned, so this mirror uses the sibling's exact rules, including its
strict RFC 4122 company-UUID form. Reconcile against the broker source when
the sibling lands on main; do not loosen this mirror independently.

The one check only provisioning can make is the ``companies/`` traverse guard.
Every company's ``requests/`` is group-writable under one shared requesting
group by design, so cross-company ingress isolation rests solely on
``companies/`` granting nothing to group or other (``0700``). The broker's
ingress walk asserts ownership and write bits but cannot detect a traverse-bit
widening: ``0751`` carries no write bit yet lets any member of the shared
group walk into every company's ``requests/``. ``validate_provisioned_tree``
therefore demands exactly ``0700`` broker-owned ``companies/`` and refuses any
widening.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

# Exact mirror of the sibling broker contract (see module docstring).
COMPANY_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
COMPANY_SPOOL_KEYS = {"companyId", "path"}
COMPANY_SPOOL_PARENT = "companies"
MAX_COMPANY_SPOOLS = 64
MAX_CONFIG_BYTES = 64 * 1024

READER_ACCOUNT = "paperclip-host-reader"
READER_GROUP = "paperclip-host-reader"
REQUESTS_GROUP = "paperclip-host-requests"

# (relative name, mode, owner, group) per company, from the reviewed layout
# contract: docs/host-ops-company-spool-routing.md on the sibling branch.
COMPANY_TREE = (
    (".", 0o750, READER_ACCOUNT, REQUESTS_GROUP),
    ("requests", 0o3730, READER_ACCOUNT, REQUESTS_GROUP),
    ("previews", 0o2750, READER_ACCOUNT, REQUESTS_GROUP),
    ("responses", 0o2750, READER_ACCOUNT, REQUESTS_GROUP),
    (".staging", 0o700, READER_ACCOUNT, READER_GROUP),
    ("processing", 0o700, READER_ACCOUNT, READER_GROUP),
    ("quarantine", 0o700, READER_ACCOUNT, READER_GROUP),
)
COMPANIES_MODE = 0o700


class Refusal(ValueError):
    pass


def validate_company_spools(value, spool):
    """Mirror of the sibling broker's exact company-ingress allow-list rules."""
    if not isinstance(value, list) or len(value) > MAX_COMPANY_SPOOLS:
        raise Refusal("companySpools must be a list of at most "
                      f"{MAX_COMPANY_SPOOLS} entries or absent")
    if not isinstance(spool, str) or not spool.startswith("/") or "\x00" in spool:
        raise Refusal("spool path must be an absolute host-owned path")
    parent = spool.rstrip("/") + "/" + COMPANY_SPOOL_PARENT
    entries = []
    seen_ids = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != COMPANY_SPOOL_KEYS:
            raise Refusal("each companySpools entry must have exactly "
                          f"{sorted(COMPANY_SPOOL_KEYS)}")
        company_id = entry["companyId"]
        if not isinstance(company_id, str) or not COMPANY_ID_RE.fullmatch(company_id):
            raise Refusal("companySpools companyId must be a canonical lowercase UUID")
        if company_id in seen_ids:
            raise Refusal("companySpools companyId must be unique")
        path = entry["path"]
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
            raise Refusal("companySpools path must be an absolute host-owned path")
        if ".." in Path(path).parts:
            raise Refusal("companySpools path must not contain ..")
        if path != f"{parent}/{company_id}":
            raise Refusal(f"companySpools path must be {parent}/<companyId>")
        # No separate duplicate-path check: the path is exactly
        # parent/companyId and ids are unique, so equal paths would imply
        # equal ids, already refused above. The reviewed broker contract has
        # no path check either; keep the mirror exact.
        seen_ids.add(company_id)
        entries.append({"companyId": company_id, "path": path})
    return entries


def render_tmpfiles_fragment(spool, entries):
    """Render an offline tmpfiles candidate for the validated company layout.

    Returns the fragment text. Refuses an empty allow-list: with no company
    the broker serves the flat spool only, so there is no fragment to render
    and emitting a bare ``companies/`` guard would imply provisioning work
    that was never requested.
    """
    validated = validate_company_spools(
        [{"companyId": e["companyId"], "path": e["path"]} for e in entries], spool)
    if not validated:
        raise Refusal("no company spools configured; nothing to provision")
    lines = [
        "# OFFLINE CANDIDATE ONLY: journaled provisioning and host evidence pending.",
        "# Rendered from the reviewed company-spool layout contract; not installed,",
        "# not validated against a host, and not a substitute for the operator run.",
    ]
    lines.append(
        f"d {spool}/{COMPANY_SPOOL_PARENT} "
        f"{COMPANIES_MODE:04o} {READER_ACCOUNT} {READER_GROUP} -"
    )
    for entry in validated:
        for name, mode, owner, group in COMPANY_TREE:
            target = entry["path"] if name == "." else f"{entry['path']}/{name}"
            lines.append(f"d {target} {mode:04o} {owner} {group} -")
    return "\n".join(lines) + "\n"


def _fixture_stat(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        raise Refusal(f"provisioning is missing {path}") from None


def validate_provisioned_tree(root, spool, entries, broker_uid, broker_gid,
                              requests_gid):
    """Check a proposed provisioned tree on a fixture root.

    All identities are explicit numeric fixture IDs; no account lookup occurs.
    Every directory must be a real directory (never a symlink) with the exact
    mode and ownership from the layout contract. A missing company root or
    view is a provisioning failure and is refused: the broker never creates
    ``requests``, ``previews``, ``responses`` or the company root.
    """
    validated = validate_company_spools(
        [{"companyId": e["companyId"], "path": e["path"]} for e in entries], spool)
    if not validated:
        raise Refusal("no company spools configured; nothing to validate")
    if type(broker_uid) is not int or type(broker_gid) is not int or \
            type(requests_gid) is not int:
        raise Refusal("fixture identities must be numeric")
    base = Path(root) / spool.lstrip("/")
    companies = base / COMPANY_SPOOL_PARENT
    info = _fixture_stat(companies)
    # Binding check the broker cannot make: any group/other traverse bit on
    # companies/ collapses cross-company isolation while every broker check
    # still passes, so the exact 0700 mode is demanded here.
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != broker_uid or \
            info.st_gid != broker_gid or stat.S_IMODE(info.st_mode) != COMPANIES_MODE:
        raise Refusal("companies/ must be an exact-0700 broker-owned directory")
    for entry in validated:
        company_root = Path(root) / entry["path"].lstrip("/")
        for name, mode, _owner, group in COMPANY_TREE:
            target = company_root if name == "." else company_root / name
            target_info = _fixture_stat(target)
            want_gid = requests_gid if group == REQUESTS_GROUP else broker_gid
            if not stat.S_ISDIR(target_info.st_mode) or \
                    target_info.st_uid != broker_uid or \
                    target_info.st_gid != want_gid or \
                    stat.S_IMODE(target_info.st_mode) != mode:
                raise Refusal(f"provisioned {name} differs from the layout contract")
    return validated


def read_config(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
            raise Refusal("provisioning config must be a bounded regular file")
        content = stream.read(MAX_CONFIG_BYTES + 1)
    if len(content) > MAX_CONFIG_BYTES:
        raise Refusal("provisioning config is too large")
    try:
        config = json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise Refusal("provisioning config is not valid JSON") from None
    if not isinstance(config, dict):
        raise Refusal("provisioning config must be an object")
    return config


def render_candidate(config, spool):
    value = config.get("companySpools")
    if value is None:
        raise Refusal("config carries no companySpools; nothing to provision")
    entries = [{"companyId": e["companyId"], "path": e["path"]}
               for e in validate_company_spools(value, spool)]
    fragment = render_tmpfiles_fragment(spool, entries)
    return {
        "kind": "offline-company-spool-provisioning",
        "schemaVersion": 1,
        "spoolPath": spool,
        "hostAcceptance": False,
        "activationReady": False,
        "companySpoolValidation": "mirror-of-reviewed-broker-contract",
        "entries": entries,
        "fragmentSha256": hashlib.sha256(fragment.encode("ascii")).hexdigest(),
        "tmpfilesFragment": fragment,
        "pending": ["journaled-provisioning-install-wiring",
                    "observed-host-provisioned-tree",
                    "trusted-launcher-company-binding",
                    "exact-head-review-and-merge"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--spool-path", required=True)
    args = parser.parse_args(argv)
    try:
        result = render_candidate(read_config(args.config), args.spool_path)
    except (Refusal, OSError):
        print("REFUSED: offline provisioning source or company contract failed",
              file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
