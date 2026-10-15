#!/usr/bin/env python3
"""check_module_wiring.py - Gatus module wiring gate (case_f3500d44).

Why this exists. "Merged module" has repeatedly meant "declared but not live":
the 2026-10-04 staleness audit (case_621b46d7) found pushed Gatus endpoints with no
push script in the repo at all, and four pushed cliproxy lanes whose script
existed but whose cron line was written nowhere, so they would have gone stale
10 minutes after wiring. ops/case_658bf6b9/MODULES.md already says Done = merged PR
plus a registry entry; nothing enforced it. This is the enforcement.

Rule. Every `external-endpoints:` entry declared in

    ops/**/gatus-*.yaml      (gatus-config.yaml, fragments, gatus-endpoints.yaml)

is a PUSHED endpoint: Gatus marks it unhealthy when no push arrives within its
heartbeat. It must therefore have all three of

  script    some ops/**/push-*.sh, push-*.py or probe-*.sh names its
            `<group>_<name>` key in CODE (comments and Python docstrings do
            not count; a key built
            from a variable counts when the literal prefix is in code, for
            example `cliproxy_lane-${lane}` or `ci_main-$slug`);
  cron      that script is referenced by a cron line in an ops/**/OPERATOR.md
            (schedule fields + the script's repo-relative path; a commented-out
            line does not count);
  registry  the endpoint is listed in ops/case_658bf6b9/MODULES.md as
            `<group>/<name>` or `<group>_<name>`.

Known gaps live in an explicit allowlist (module_wiring_allowlist.json), one
entry per endpoint naming which checks are exempt and the OWNING CARD that
closes the gap. The allowlist cannot rot: an exemption whose check has become
compliant FAILS the run (allowlist-stale), as does an entry for an endpoint
that is no longer declared (allowlist-orphan) or one without a card.

Exit status:  0 = every endpoint wired (or exempted with an owning card)
              1 = at least one violation
              2 = nothing measurable (no endpoints found, unreadable registry
                  or allowlist): never reported as a pass.

Stdlib only. Offline. Read-only: it reads repo files and writes nothing.
"""
import argparse
import ast
import json
import re
import sys
from pathlib import Path

CHECKS = ("script", "cron", "registry")
CODE_FOR = {"script": "missing-script", "cron": "missing-cron", "registry": "missing-registry"}
ALLOWLIST_SCHEMA = "case_577d9037-module-wiring-allowlist-v1"
REGISTRY_REL = "ops/case_658bf6b9/MODULES.md"
ALLOWLIST_REL = "ops/case_658bf6b9/module_wiring_allowlist.json"
SKIP_DIRS = {"fixtures", ".git", "node_modules"}

# Gatus derives an external endpoint's push key from group and name; both are
# lowercased, trimmed and have these separators mapped to '-'.
_SANITIZE = str.maketrans({c: "-" for c in "/_., #+&"})


def sanitize(s):
    return s.strip().lower().translate(_SANITIZE)


def endpoint_key(group, name):
    return sanitize(group) + "_" + sanitize(name)


def contract_finding(script, line, pushed, group, name):
    """None when a push script's key matches the Gatus-derived key, else a finding.

    `script` is the pusher's repo-relative path, `line` the gatus_push call
    site (or 0 when the key is built dynamically), `pushed` the key the
    script actually pushes, and (`group`, `name`) the endpoint the push is
    for (from the script's --emit-gatus-yaml or the declared stanza).
    A mismatch names `script:line` and the expected key, so the failure
    points at the pusher, not the registry.
    """
    expected = endpoint_key(group, name)
    if pushed == expected:
        return None
    at = f"{script}:{line}" if line else script
    return (f"{at} pushes `{pushed}` but Gatus keys {group}/{name} as "
            f"`{expected}` (expected key `{expected}`)")


def _walk(root, pattern):
    """rglob under <root>/ops, skipping fixture trees; sorted for stable output."""
    base = root / "ops"
    if not base.is_dir():
        return []
    out = []
    for p in base.rglob(pattern):
        rel = p.relative_to(root)
        if p.is_file() and not (set(rel.parts) & SKIP_DIRS):
            out.append(p)
    return sorted(out)


def _scalar(v):
    v = v.strip()
    if v[:1] in ("'", '"') and v.count(v[0]) >= 2:
        return v[1:v.index(v[0], 1)]
    return re.sub(r"\s+#.*$", "", v).strip()


def parse_external_endpoints(text):
    """Minimal scan of the top-level `external-endpoints:` list.

    Returns a list of {"line", "name", "group"} dicts. Only the block's own
    item keys (one level below the `-`) are read, so nested alert/heartbeat
    keys can never be mistaken for the endpoint's name or group.
    """
    entries = []
    in_block = False
    cur = None
    item_indent = None

    def take(body):
        m = re.match(r"^(name|group)\s*:\s*(.*)$", body)
        if m and cur is not None:
            cur[m.group(1)] = _scalar(m.group(2))

    for i, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0 and not stripped.startswith("- "):
            in_block = re.match(r"^external-endpoints\s*:", raw) is not None
            cur, item_indent = None, None
            continue
        if not in_block:
            continue
        m = re.match(r"^(\s*)-\s+(.*)$", raw)
        if m and (item_indent is None or len(m.group(1)) == item_indent):
            item_indent = len(m.group(1))
            cur = {"line": i, "name": None, "group": None}
            entries.append(cur)
            take(m.group(2))
        elif cur is not None and indent == item_indent + 2:
            take(stripped)
    return entries


def _code_text(script_text, suffix=".sh"):
    """Source without full-line shell comments or Python comments/docstrings.

    Python is parsed, never executed. Invalid Python supplies no wiring evidence.
    """
    if suffix == ".py":
        try:
            tree = ast.parse(script_text)
        except SyntaxError:
            return ""
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    node.body.pop(0)
        return ast.unparse(tree)
    return "\n".join(l for l in script_text.splitlines() if not l.lstrip().startswith("#"))


# `prefix` immediately followed by a shell expansion: the key is built at run
# time and only the literal prefix is statically visible.
_TEMPLATE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9_-]*)(?:\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\$\([^)]*\))"
)


def script_names_key(code, key, group):
    lit = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(key) + r"(?![A-Za-z0-9_-])")
    if lit.search(code):
        return True
    floor = len(sanitize(group)) + 1  # prefix must reach past `<group>_`
    for m in _TEMPLATE.finditer(code):
        prefix = m.group(1)
        if len(prefix) > floor and prefix.startswith(sanitize(group) + "_") and key.startswith(prefix):
            return True
    return False


_FIELD = r"[0-9*/,\-]+"
_CRON = re.compile(
    r"(?:^|[\s`])(?:@(?:reboot|yearly|annually|monthly|weekly|daily|midnight|hourly)"
    r"|(?:" + _FIELD + r"\s+){4}[0-9A-Za-z*/,\-]+)\s"
)


def cron_line_runs(lines, rel):
    """True when some active cron line runs the script at repo-relative `rel`."""
    for raw in lines:
        if raw.lstrip().startswith("#"):
            continue
        m = _CRON.search(raw)
        if m and re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(rel) + r"(?![A-Za-z0-9_.-])", raw[m.end():]):
            return True
    return False


def registry_lists(reg_text, group, name):
    text = re.sub(r"<!--.*?-->", "", reg_text, flags=re.S)
    forms = {f"{group}/{name}", f"{group}_{name}",
             f"{sanitize(group)}/{sanitize(name)}", f"{sanitize(group)}_{sanitize(name)}"}
    for f in forms:
        if re.search(r"(?<![A-Za-z0-9_/-])" + re.escape(f) + r"(?![A-Za-z0-9_-])", text):
            return True
    return False


def load_allowlist(path):
    """Returns (entries, error). Missing file is an empty allowlist."""
    if not path.exists():
        return [], None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"allowlist unreadable: {e}"
    if not isinstance(doc, dict) or doc.get("schema") != ALLOWLIST_SCHEMA \
            or not isinstance(doc.get("entries"), list):
        return None, f"allowlist schema is not {ALLOWLIST_SCHEMA}"
    return doc["entries"], None


def evaluate(root, registry=None, allowlist=None):
    """Returns (findings, report, exit_code). findings: list of (code, key, detail)."""
    root = Path(root)
    registry = Path(registry) if registry else root / REGISTRY_REL
    allowlist = Path(allowlist) if allowlist else root / ALLOWLIST_REL
    findings = []

    configs = _walk(root, "gatus-*.yaml") + _walk(root, "gatus-*.yml")
    endpoints = []  # (key, group, name, rel)
    for cfg in configs:
        rel = cfg.relative_to(root).as_posix()
        try:
            text = cfg.read_text(encoding="utf-8")
        except OSError as e:
            return [("unreadable", rel, str(e))], [], 2
        for ent in parse_external_endpoints(text):
            if not ent["name"] or not ent["group"]:
                findings.append(("malformed-endpoint", f"{rel}:{ent['line']}",
                                 "external endpoint needs both `name` and `group` (the push key is <group>_<name>)"))
                continue
            endpoints.append((endpoint_key(ent["group"], ent["name"]), ent["group"], ent["name"], rel))
    if not endpoints and not findings:
        return [("nothing-measured", "-", "no external-endpoints entries found under ops/**/gatus-*.yaml")], [], 2

    try:
        reg_text = registry.read_text(encoding="utf-8")
    except OSError as e:
        return [("unreadable", registry.as_posix(), f"registry unreadable: {e}")], [], 2
    entries, err = load_allowlist(allowlist)
    if err:
        return [("allowlist-invalid", allowlist.as_posix(), err)], [], 2

    scripts = {}
    for p in sorted({p for pattern in ("push-*.sh", "push-*.py", "probe-*.sh")
                     for p in _walk(root, pattern)}):
        try:
            scripts[p.relative_to(root).as_posix()] = _code_text(
                p.read_text(encoding="utf-8", errors="replace"), p.suffix)
        except OSError:
            continue
    op_lines = []
    for p in _walk(root, "OPERATOR.md"):
        op_lines.extend(p.read_text(encoding="utf-8", errors="replace").splitlines())

    declared = {}
    state = {}  # key -> {check: bool}
    detail = {}
    for key, group, name, rel in endpoints:
        declared[key] = rel
        named_by = [s for s, code in scripts.items() if script_names_key(code, key, group)]
        wired = [s for s in named_by if cron_line_runs(op_lines, s)]
        state[key] = {
            "script": bool(named_by),
            "cron": bool(wired),
            "registry": registry_lists(reg_text, group, name),
        }
        detail[key] = {
            "script": f"no ops/**/push-*.sh, push-*.py or probe-*.sh names `{key}` in code",
            "cron": ("no OPERATOR.md cron line runs " + ", ".join(named_by)) if named_by
                    else f"no push script, so nothing for a cron line to run",
            "registry": f"{REGISTRY_REL} does not list {group}/{name}",
        }

    allowed = {}
    seen = set()
    for ent in entries:
        key = ent.get("key") if isinstance(ent, dict) else None
        if not isinstance(key, str):
            findings.append(("allowlist-invalid", "-", "entry without a string `key`"))
            continue
        if key in seen:
            findings.append(("allowlist-duplicate", key, "listed twice; merge into one entry"))
            continue
        seen.add(key)
        exempt = ent.get("exempt")
        card = ent.get("card")
        if not (isinstance(exempt, list) and exempt and all(c in CHECKS for c in exempt)):
            findings.append(("allowlist-bad-exempt", key, f"`exempt` must be a non-empty subset of {list(CHECKS)}"))
            continue
        if not (isinstance(card, str) and re.fullmatch(r"(?:TOG-\d+|case_[0-9a-f]{8})", card)):
            findings.append(("allowlist-no-card", key, "every exemption must name its owning card (case_<8 lowercase hex> or TOG-<n>)"))
            continue
        if key not in declared:
            findings.append(("allowlist-orphan", key, f"{card}: endpoint is no longer declared; delete the entry"))
            continue
        allowed[key] = (set(exempt), card)

    report = []
    for key in sorted(declared):
        exempt, card = allowed.get(key, (set(), None))
        for chk in CHECKS:
            ok = state[key][chk]
            if chk in exempt:
                if ok:
                    findings.append(("allowlist-stale", key,
                                     f"`{chk}` is now compliant but still exempt under {card}; remove it from the allowlist"))
                else:
                    report.append(f"ALLOWED {key}: {CODE_FOR[chk]} (owner {card})")
            elif not ok:
                findings.append((CODE_FOR[chk], key, detail[key][chk]))
        if all(state[key].values()):
            report.append(f"OK      {key}")
    return findings, report, 1 if findings else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]),
                    help="repository root (default: this checkout)")
    ap.add_argument("--registry", help=f"module registry (default <root>/{REGISTRY_REL})")
    ap.add_argument("--allowlist", help=f"allowlist (default <root>/{ALLOWLIST_REL})")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    findings, report, rc = evaluate(args.root, args.registry, args.allowlist)
    if args.json:
        print(json.dumps({"exit": rc,
                          "findings": [{"code": c, "key": k, "detail": d} for c, k, d in findings],
                          "report": report}, indent=2))
    else:
        for line in report:
            print(line)
        for c, k, d in findings:
            print(f"FAIL    {k}: {c} - {d}")
        print(("module wiring gate: PASS" if rc == 0 else
               "module wiring gate: NOT MEASURED" if rc == 2 else
               f"module wiring gate: FAIL ({len(findings)} finding(s))"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
