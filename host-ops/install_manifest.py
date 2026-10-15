#!/usr/bin/env python3
"""Offline pinned-bundle manifest for the reviewed host-ops installer export.

Reads the reviewed commit only: the export-member list is parsed from the
shipped ``host-ops/install.sh`` in that commit (never retyped here), the
fixed install inventory is imported from the reviewed ``install_state.py``
bytes, and every member's blob identity comes from ``ls-tree``. Every file
in the reviewed ``host-ops/`` tree must be accounted for -- a journaled
install destination, a staged rollback helper, the config seed, the sourced
lib, the operator entrypoint, the first-install preparation entrypoint, or
an offline tool -- and every inventory
destination must resolve to reviewed bytes or the explicitly host-held key.
Anything else refuses.

This emits the reviewable manifest and the exact operator install/rollback
commands. It never touches the host, never reads keys or spool state, and is
not staged into the install journal or executed by the installer.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REF_RE = re.compile(r"^[0-9a-f]{40}$")
# Exactly one export loop in the shipped installer; members are bare names.
EXPORT_LOOP_RE = re.compile(r"^for source in (.+); do$", re.MULTILINE)
MAX_MEMBER_BYTES = 2 * 1024 * 1024
# The operator runs this file from a checkout of the reviewed commit; it is
# the script whose export loop this manifest parses, so it is definitionally
# present. It is never exported into the install workdir.
OPERATOR_ENTRYPOINT = "install.sh"
# Fresh-host preparation entrypoint: operator-run as root, creates the broker
# identity and protected directories the upgrade-only installer requires.
# It is never exported into the install workdir, never staged into the
# journal and never installed -- classified here so a future unlisted file
# still refuses instead of riding along with it.
PREPARATION_ENTRYPOINT = "prepare-first-install.sh"
# Reviewed offline preparation tools: standalone CLIs invoked only by the
# offline test gate, never staged into the journal, never installed, never
# imported by the installer path. A new offline tool requires extending this
# tuple under review -- an unlisted archive member refuses, never rides
# along silently. This manifest tool lists itself here: it is the same kind
# of offline CLI (reads reviewed bytes, emits a review artifact, runs only
# in the test gate and reviewer hands), and self-listing keeps the
# ride-along refusal total -- a future unlisted file still refuses.
OFFLINE_TOOLS = ("spool_carrier.py", "spool_mapping.py", "install_manifest.py")
# Export members that are not journaled install destinations: the staged
# rollback helpers (exact set the transaction publishes into the journal),
# the config seed the installer copies when no operator config exists, the
# templates rendered into staged temporaries (sudoers via two shipped
# renderers plus visudo, service via staged copy plus rewrite plus
# systemd-analyze verify), and the lib the installer sources renderers from.
STAGED_HELPERS = ("install_state.py", "install_transaction.py",
                  "spool_provisioning.py")
CONFIG_SEED = "config.example.json"
# The sudoers template is rendered into a staged temp (two shipped renderers
# plus visudo) and the transaction publishes those staged bytes, never the
# raw template. The service template is ALSO staged and rewritten -- but only
# for pre-validation (systemd-analyze verify); the transaction publishes the
# raw export bytes (sources[name] is args.source_dir/name for the unit), so
# the service template is an install-destination, not a rendered template.
RENDERED_TEMPLATES = ("host-ops-broker.sudoers",)
SOURCED_LIB = "install-lib.sh"
# Inventory destinations with no reviewed bytes: rendered at install time
# from the pinned template, or explicitly host-held and never published.
RENDERED_SOURCES = {"sudoers": "host-ops-broker.sudoers",
                    "sudoers-mirror": "host-ops-broker.sudoers",
                    "config": "config.example.json"}
HOST_HELD_KEYS = {"public-key"}
FIXED_PUBLIC_KEY_OUT = "/etc/paperclip-host-ops/response-signing.pub.pem"


class Refusal(ValueError):
    pass


def git(repo, *args, timeout=60):
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(repo), *args],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError):
        raise Refusal("reviewed git object is unreadable") from None
    return result.stdout


def reviewed_blob(repo, ref, path):
    raw = git(repo, "cat-file", "-p", f"{ref}:{path}")
    if len(raw) > MAX_MEMBER_BYTES:
        raise Refusal(f"reviewed {path} exceeds size bound")
    return raw


def export_members(repo, ref):
    """Parse the installer export loop from the reviewed commit itself."""
    try:
        script = reviewed_blob(repo, ref, "host-ops/install.sh").decode("utf-8")
    except UnicodeError:
        raise Refusal("reviewed host-ops/install.sh is not text") from None
    loops = EXPORT_LOOP_RE.findall(script)
    if len(loops) != 1:
        raise Refusal("reviewed installer export loop is not exactly one")
    members = loops[0].split()
    if len(set(members)) != len(members) or any(
            "/" in name or name.startswith(".") for name in members):
        raise Refusal("reviewed installer export list is not plain unique names")
    return members


def archive_members(repo, ref):
    """Every top-level file in the reviewed host-ops/ tree, via ls-tree."""
    raw = git(repo, "ls-tree", "-r", "--name-only", "-z", ref, "host-ops/")
    try:
        names = [n for n in raw.decode("ascii").split("\0") if n]
    except UnicodeDecodeError:
        raise Refusal("reviewed host-ops tree listing is unreadable") from None
    top = []
    for path in names:
        parts = Path(path).parts
        if len(parts) != 2 or parts[0] != "host-ops":
            raise Refusal("reviewed host-ops tree has unexpected layout")
        top.append(parts[1])
    if len(set(top)) != len(top) or any(
            "/" in name or name.startswith(".") or not name for name in top):
        raise Refusal("reviewed host-ops tree is not plain unique names")
    return sorted(top)


def member_record(repo, ref, name):
    """Blob identity and content hash of one reviewed tree member."""
    try:
        listing = git(repo, "ls-tree", ref, f"host-ops/{name}").decode("ascii")
    except UnicodeDecodeError:
        raise Refusal(f"reviewed host-ops/{name} listing is unreadable") from None
    if not listing:
        raise Refusal(f"reviewed host-ops/{name} is missing from the commit")
    # ls-tree separates the pathname with a tab; split there so a hostile
    # name cannot shift the mode/type/sha columns.
    left, _, _ = listing.partition("\t")
    fields = left.split()
    if len(fields) != 3 or fields[1] != "blob" or not fields[0].startswith("100"):
        raise Refusal(f"reviewed host-ops/{name} is not a regular file")
    blob_sha = fields[2]
    content = git(repo, "cat-file", "blob", blob_sha)
    if len(content) > MAX_MEMBER_BYTES:
        raise Refusal(f"reviewed host-ops/{name} exceeds size bound")
    return {"name": name, "mode": fields[0], "blobSha": blob_sha,
            "sha256": hashlib.sha256(content).hexdigest(),
            "sizeBytes": len(content), "_content": content}


def import_files_inventory(install_state_content, workdir):
    """Import FILES/PUBLIC_OUT from reviewed install_state.py bytes."""
    module_path = Path(workdir) / "reviewed-install_state.py"
    with open(module_path, "xb") as stream:
        stream.write(install_state_content)
    sys.dont_write_bytecode = True
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location(
            "reviewed_install_state", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        files = dict(module.FILES)
        public_out = str(module.PUBLIC_OUT)
    except (OSError, ValueError, AttributeError, ImportError, SyntaxError):
        raise Refusal("reviewed install inventory is unreadable") from None
    finally:
        try:
            module_path.unlink()
        except OSError:
            pass
    if not files or any("/" in name or not dest.startswith("/")
                         for name, dest in files.items()):
        raise Refusal("reviewed install inventory shape is invalid")
    return files, public_out


def coverage(files, loop, names):
    """Account every reviewed tree member and every inventory destination."""
    loop = list(loop)
    missing = set(loop) - set(names)
    if missing:
        raise Refusal("reviewed export loop names a file missing from the "
                      f"commit: {sorted(missing)[0]}")
    # The rollback entrypoint re-executes the transaction from the journal,
    # so the loop must stage exactly the helper set the transaction imports.
    # Checked on the loop (not the coverage table) so a dropped helper
    # refuses here with its own message rather than the generic one below.
    if sorted(n for n in loop if n in STAGED_HELPERS) != sorted(STAGED_HELPERS):
        raise Refusal("reviewed export does not stage the exact helper set")
    table = {}
    for name in names:
        if name in loop:
            if name in files:
                table[name] = {"kind": "install-destination",
                               "destination": files[name]}
            elif name in STAGED_HELPERS:
                table[name] = {"kind": "staged-rollback-helper"}
            elif name == CONFIG_SEED:
                table[name] = {"kind": "config-seed"}
            elif name in RENDERED_TEMPLATES:
                table[name] = {"kind": "rendered-template"}
            elif name == SOURCED_LIB:
                table[name] = {"kind": "sourced-lib"}
            else:
                raise Refusal(f"reviewed export member {name} has no install use")
        elif name in STAGED_HELPERS or name == CONFIG_SEED or \
                name in RENDERED_TEMPLATES or name == SOURCED_LIB or \
                name in files:
            # A known install input that the export loop no longer ships:
            # the installer would run without it (helpers) or publish
            # without it (destinations), so this refuses with its own
            # message rather than the generic ride-along one.
            raise Refusal(f"reviewed host-ops/{name} is not exported by the installer")
        elif name == OPERATOR_ENTRYPOINT:
            table[name] = {"kind": "operator-entrypoint"}
        elif name == PREPARATION_ENTRYPOINT:
            table[name] = {"kind": "preparation-entrypoint"}
        elif name in OFFLINE_TOOLS:
            table[name] = {"kind": "offline-tool"}
        else:
            raise Refusal(f"reviewed host-ops/{name} rides along unaccounted")
    destinations = {}
    for key, dest in files.items():
        if key in loop:
            destinations[key] = {"kind": "reviewed-bytes", "source": key,
                                 "destination": dest}
        elif key in RENDERED_SOURCES:
            source = RENDERED_SOURCES[key]
            if source not in loop:
                raise Refusal(f"inventory {key} renders from missing {source}")
            destinations[key] = {"kind": "rendered-at-install",
                                 "source": source, "destination": dest}
        elif key in HOST_HELD_KEYS:
            destinations[key] = {"kind": "host-held-key",
                                 "destination": dest}
        else:
            raise Refusal(f"inventory destination {key} has no reviewed source")
    return table, destinations


def build_manifest(repo, ref, workdir):
    if not REF_RE.fullmatch(ref):
        raise Refusal("source ref must be a 40-hex commit")
    if git(repo, "cat-file", "-t", ref).decode("ascii").strip() != "commit":
        raise Refusal("source ref must name a commit object directly")
    try:
        git(repo, "cat-file", "-e", f"{ref}:host-ops/broker.py")
    except Refusal:
        raise Refusal("reviewed commit has no host-ops/broker.py") from None
    loop = export_members(repo, ref)
    names = archive_members(repo, ref)
    records = [member_record(repo, ref, name) for name in names]
    by_name = {r["name"]: r for r in records}
    files, public_out = import_files_inventory(
        by_name["install_state.py"]["_content"], workdir)
    if public_out != FIXED_PUBLIC_KEY_OUT:
        raise Refusal("reviewed public-key destination is not the fixed journaled path")
    table, destinations = coverage(files, loop, names)
    canonical = "\n".join(f"{r['name']}:{r['sha256']}" for r in records) + "\n"
    install_command = (
        "sudo ./host-ops/install.sh"
        f" --source-ref {ref}"
        f" --public-key-out {FIXED_PUBLIC_KEY_OUT}"
        " --expected-public-key-sha256 <OPERATOR: sha256 of the host-held "
        "/var/lib/paperclip-host-ops/keys/response-signing.pub.pem, recorded independently>"
        " --cliproxy-unit <OPERATOR: exact .service unit captured from the host>"
        " [--paperclip-image-ref <optional equality assertion, never an override>]"
    )
    for record in records:
        del record["_content"]
    return {
        "kind": "host-ops-install-bundle-manifest",
        "schemaVersion": 1,
        "sourceRef": ref,
        "hostAcceptance": False,
        "activationReady": False,
        "writesEnabled": False,
        "exportMembers": records,
        "filesSha256": hashlib.sha256(canonical.encode("ascii")).hexdigest(),
        "exportCoverage": table,
        "installDestinations": destinations,
        "installCommand": install_command,
        "preparationCommand":
            "sudo ./host-ops/prepare-first-install.sh [--expect-requests-members <csv>]"
            "  # fresh hosts only, before the operator key step and install.sh;"
            " takes only the adoption-assertion flag, refuses on any existing"
            " identity/key/service state outside its narrow adoption rules,"
            " adopts a pre-existing requests group only when the observed"
            " members equal the operator-supplied expected list (no flag means"
            " a group with members refuses), pins both lists in the journal"
            " (installer refuses later drift, including primary-group holders),"
            " and prints its journal and explicit rollback lines",
        "rollbackCommandShape":
            "/usr/bin/python3 -B <INSTALL-JOURNAL>/install_transaction.py"
            " rollback --state <INSTALL-JOURNAL>",
        "rollbackNote":
            "The concrete journal path is emitted by the installer as its "
            "INSTALL JOURNAL / ROLLBACK lines; it cannot be pre-pinned "
            "because the journal token is random per run. If the install "
            "fails before emitting a path, inspect the protected journal "
            "root; never invent, wildcard, or reuse a prior journal path.",
        "pending": ["host-captured cliproxy unit name",
                    "independently recorded public-key hash",
                    "operator pre-state capture (unit/carrier/config/service)",
                    "exact-head review and non-author merge"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--workdir", default=None)
    args = parser.parse_args(argv)
    try:
        repo = Path(args.repo)
        if not repo.is_dir():
            raise Refusal("repo must be a directory")
        if args.workdir is None:
            with tempfile.TemporaryDirectory(prefix="host-ops-manifest-") as tmp:
                manifest = build_manifest(repo, args.source_ref, tmp)
        else:
            manifest = build_manifest(repo, args.source_ref, args.workdir)
    except Refusal as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2
    except OSError:
        print("REFUSED: offline manifest workspace failed", file=sys.stderr)
        return 2
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
