#!/usr/bin/env python3
"""Pre-switch gate for the CLIProxy 8.0.8 -> 8.0.10 binary swap (case_80989a33).

Run by the operator against PRIVATE COPIES of the live config.yaml, before the
swap. Read-only, offline, no network. Never prints a config VALUE: the file
carries client and upstream keys, so output is key paths, booleans and sha256.

Two questions:

1. What do the v8.0.9 `client.codex` flags resolve to once 8.0.10 loads this
   file? Upstream migrates legacy spellings at load time (config_v8.go
   `v8ClientPaths` @ v8.0.10): `client.codex.optimize-multi-agent-v2` wins by
   PRESENCE, including explicit false and null; when it is absent the old
   `oauth.providers.codex...` path wins, then `providers.codex...`, then
   `codex...`. `enable-apply-patch` has no legacy spelling. The card keeps both
   flags OFF at the switch; a legacy `true` would silently keep one ON.

2. With `--baseline`, is the candidate the same file? The runbook reuses the
   live config unchanged. Any difference is listed by key path only and needs
   a reviewed, explicit delta.

Exit codes: 0 both flags false and (if --baseline) byte-identical;
2 unreadable/not a mapping; 3 a flag resolves true; 4 candidate differs from
baseline. 3 wins over 4.
"""

import argparse
import hashlib
import sys

import yaml

FLAG_MULTI_AGENT = "client.codex.optimize-multi-agent-v2"
FLAG_APPLY_PATCH = "client.codex.enable-apply-patch"

# Order is upstream precedence after the canonical client path.
LEGACY_MULTI_AGENT = (
    "oauth.providers.codex.optimize-multi-agent-v2",
    "providers.codex.optimize-multi-agent-v2",
    "codex.optimize-multi-agent-v2",
)

_MISSING = object()


class GateError(Exception):
    pass


def load(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        # The problem mark carries a line/column, never the offending text.
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise GateError(f"{path}: YAML parse error{where}") from None
    if not isinstance(doc, dict):
        raise GateError(f"{path}: top level is not a mapping")
    return raw, doc


def lookup(doc, dotted):
    node = doc
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def as_flag(value, path):
    # Upstream treats null as the default (false). Anything else that is not a
    # YAML boolean is not something this gate will guess about.
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise GateError(f"{path}: not a boolean; resolve by hand before the switch")


def resolve_multi_agent(doc):
    for path in (FLAG_MULTI_AGENT,) + LEGACY_MULTI_AGENT:
        value = lookup(doc, path)
        if value is not _MISSING:
            return as_flag(value, path), path
    return False, "default"


def resolve_apply_patch(doc):
    value = lookup(doc, FLAG_APPLY_PATCH)
    if value is _MISSING:
        return False, "default"
    return as_flag(value, FLAG_APPLY_PATCH), FLAG_APPLY_PATCH


def leaves(node, prefix=""):
    """Map every leaf key path to its value. Values stay in memory only."""
    out = {}
    if isinstance(node, (dict, list)) and not node:
        out[prefix] = node
    elif isinstance(node, dict):
        for key, child in node.items():
            out.update(leaves(child, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(node, list):
        for i, child in enumerate(node):
            out.update(leaves(child, f"{prefix}[{i}]"))
    else:
        out[prefix] = node
    return out


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("candidate", help="private copy of the config.yaml 8.0.10 will load")
    ap.add_argument("--baseline", help="private copy of the live 8.0.8 config.yaml")
    args = ap.parse_args(argv)

    try:
        raw, doc = load(args.candidate)
        multi, multi_src = resolve_multi_agent(doc)
        patch, patch_src = resolve_apply_patch(doc)
        base = load(args.baseline) if args.baseline else None
    except (OSError, GateError) as exc:
        msg = str(exc) if isinstance(exc, GateError) else f"cannot read: {exc.filename}"
        print(f"UNREADABLE {msg}", file=sys.stderr)
        return 2

    status = 0
    print(f"candidate sha256={sha256(raw)}")
    for name, value, src in (
        (FLAG_MULTI_AGENT, multi, multi_src),
        (FLAG_APPLY_PATCH, patch, patch_src),
    ):
        print(f"{name}={str(value).lower()} source={src}")
        if value:
            status = 3
    if multi_src in LEGACY_MULTI_AGENT:
        print(f"NOTE legacy spelling {multi_src} is migrated at load; "
              f"a management write would refuse it")

    if base is not None:
        base_raw, base_doc = base
        print(f"baseline  sha256={sha256(base_raw)}")
        if base_raw != raw:
            old, new = leaves(base_doc), leaves(doc)
            added = sorted(set(new) - set(old))
            removed = sorted(set(old) - set(new))
            changed = sorted(p for p in set(old) & set(new) if old[p] != new[p])
            for label, paths in (("added", added), ("removed", removed), ("changed", changed)):
                for p in paths:
                    print(f"DELTA {label} {p}")
            if not (added or removed or changed):
                print("DELTA bytes only (comments/layout); parsed values identical")
            status = status or 4

    print({0: "PASS", 3: "FAIL flag-on", 4: "FAIL delta-needs-review"}[status])
    return status


if __name__ == "__main__":
    sys.exit(main())
