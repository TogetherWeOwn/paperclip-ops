#!/usr/bin/env python3
"""Compare exact Git snapshots using the trusted AX-lint producer; stdout is JSON."""
import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

POLICY = ".diagnostic-delta.json"
PRODUCER = Path(__file__).resolve().parents[2] / "templates/org-github/ax-lint/ax_lint.py"
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
MAX_REPORT = 1024 * 1024
MAX_SOURCE = 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_PATHS = 10000
RULES = {
    "agents-map/missing": "agents-map",
    "agents-map/oversize": "agents-map",
    "script-surface/no-json-or-dry-run": "script-surface",
    "dispatch-inputs/untyped": "dispatch-inputs",
    "dispatch-inputs/choice-without-options": "dispatch-inputs",
}
# Keep the producer's documented exclusions explicit, not silently certified.
EXCLUDED = {".git", "node_modules", "vendor", ".venv", "venv", "__pycache__",
            ".tox", "target", "dist", "build", ".mypy_cache", ".pytest_cache", ".ax-source"}


class Unmeasurable(Exception):
    def __init__(self, code, next_action, retryable=False):
        self.code = code
        self.next_action = next_action
        self.retryable = retryable


def refuse(code, action):
    raise Unmeasurable(code, action)


def safe_path(value):
    if (not isinstance(value, str) or not value or value.startswith("/")
            or "\\" in value or "\x00" in value
            or any(part in ("", ".", "..", ".git") for part in value.split("/"))):
        refuse("unsafe_path", "Use canonical repository-relative POSIX paths without dot segments or symlinks.")
    return value


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def json_value(raw, limit, code):
    if not isinstance(raw, (str, bytes)) or len(raw) > limit:
        refuse(code, "Supply a complete, bounded UTF-8 JSON report or policy.")
    try:
        return json.loads(raw, object_pairs_hook=object_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise Unmeasurable(code, "Supply valid JSON with unique keys; rerun the trusted checker.") from exc


def git(repo, *args, input=None, limit=16 * 1024 * 1024):
    # No replace objects, caller Git environment, global config, shell, filters,
    # checkout hooks or working-tree reads. Blob bytes come straight from Git.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
            result = subprocess.run(["git", "-C", str(repo), *args], input=input,
                                    stdout=output, stderr=error, timeout=30, env=env)
            output.seek(0)
            raw = output.read(limit + 1)
    except subprocess.TimeoutExpired as exc:
        raise Unmeasurable("git_timeout", "Retry with complete history on a healthy runner.", True) from exc
    except OSError as exc:
        raise Unmeasurable("git_unavailable", "Install Git and select a readable repository.") from exc
    if result.returncode or len(raw) > limit:
        refuse("git_failed", "Fetch the exact commits; keep the source within the documented snapshot limits.")
    return raw


def tree_at(repo, sha):
    raw = git(repo, "ls-tree", "-r", "-l", "-z", sha)
    entries = {}
    try:
        for row in raw.split(b"\0"):
            if not row:
                continue
            meta, name = row.split(b"\t", 1)
            mode, kind, oid, size = meta.split()
            path = safe_path(name.decode("utf-8"))
            if not SHA.fullmatch(oid.decode("ascii")) or path in entries:
                raise ValueError("tree entry")
            entries[path] = (mode, kind, oid, int(size) if size != b"-" else None)
    except (ValueError, UnicodeError) as exc:
        raise Unmeasurable("invalid_tree", "Use a valid UTF-8 Git tree with canonical paths.") from exc
    if len(entries) > 100000:
        refuse("snapshot_limit", "Reduce the snapshot below 100000 tracked entries.")
    return entries


def blobs(repo, entries):
    if not entries:
        return {}
    # ls-tree supplies sizes before cat-file: cap allocation and never apply
    # smudge filters, export attributes or consumer-controlled generators.
    unique = {entry[2]: entry[3] for entry in entries.values()}
    if any(size is None or size > MAX_SOURCE for size in unique.values()) or sum(unique.values()) > MAX_TOTAL:
        refuse("snapshot_limit", "Keep each scoped source under 1 MiB and total scoped blobs under 32 MiB.")
    raw = git(repo, "cat-file", "--batch", input=b"\n".join(unique) + b"\n",
              limit=MAX_TOTAL + len(unique) * 256)
    contents = {}
    offset = 0
    for oid, size in unique.items():
        end = raw.find(b"\n", offset)
        expected = oid + b" blob " + str(size).encode()
        if end < 0 or raw[offset:end] != expected:
            refuse("invalid_blob", "Fetch complete source blobs and retry.")
        offset = end + 1
        value = raw[offset:offset + size]
        offset += size
        if len(value) != size or raw[offset:offset + 1] != b"\n":
            refuse("invalid_blob", "Fetch complete source blobs and retry.")
        offset += 1
        contents[oid] = value
    if offset != len(raw):
        refuse("invalid_blob", "Inspect unexpected Git batch output.")
    return {path: contents[entry[2]] for path, entry in entries.items()}


def policy_at(repo, tree):
    if POLICY not in tree:
        return None
    entry = tree[POLICY]
    if entry[:2] != (b"100644", b"blob") or entry[3] > 65536:
        refuse("invalid_policy", "Commit a regular version-1 .diagnostic-delta.json under 64 KiB in the trusted base.")
    value = json_value(blobs(repo, {POLICY: entry})[POLICY], 65536, "invalid_policy")
    if (not isinstance(value, dict) or set(value) != {"version", "producer", "include"}
            or type(value["version"]) is not int or value["version"] != 1
            or value["producer"] != "ax-lint" or not isinstance(value["include"], list)
            or not 1 <= len(value["include"]) <= 64):
        refuse("invalid_policy", "Use version=1, producer=ax-lint and 1..64 safe include globs, with no extra fields.")
    for pattern in value["include"]:
        safe_path(pattern)
        if len(pattern) > 256:
            refuse("invalid_policy", "Limit each repository-relative include glob to 256 characters.")
    return value["include"]


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def scope_ancestor(path, patterns):
    # Conservatively refuse symlink/submodule ancestors of wildcard scopes.
    for pattern in patterns:
        prefix = re.split(r"[*?\[]", pattern, maxsplit=1)[0].rstrip("/")
        if not prefix or path == prefix or prefix.startswith(path + "/") or path.startswith(prefix + "/"):
            return True
    return False


def snapshot(repo, tree, patterns, root):
    selected = {}
    for path, entry in tree.items():
        regular = entry[:2] in ((b"100644", b"blob"), (b"100755", b"blob"))
        if not regular:
            if matches(path, patterns) or scope_ancestor(path, patterns):
                refuse("unsupported_source", "Replace scoped symlinks/submodules with regular readable source files.")
            continue
        if matches(path, patterns):
            if any(part in EXCLUDED for part in path.split("/")[:-1]):
                refuse("unsupported_source", "Exclude producer-ignored directories explicitly; AX-lint cannot measure them.")
            selected[path] = entry
    if len(selected) > MAX_PATHS or sum(entry[3] for entry in selected.values()) > MAX_TOTAL:
        refuse("snapshot_limit", "Limit scope to 10000 files and 32 MiB of source.")
    source = blobs(repo, selected)
    for path, raw in source.items():
        try:
            raw.decode("utf-8")
        except UnicodeError as exc:
            raise Unmeasurable("unreadable_source", "Use UTF-8 text for scoped source files.") from exc
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        target.chmod(0o755 if selected[path][0] == b"100755" else 0o644)
    return source


def line_number(value):
    return value is None or type(value) is int and value > 0


def empty_skip(entry, sources):
    raw = sources.get(entry["path"])
    if raw is None or entry["line"] is None:
        return False
    lines = raw.decode("utf-8").splitlines()
    if entry["line"] > len(lines):
        return False
    line = lines[entry["line"] - 1]
    # Only explicitly empty forms are measurable omissions. Never accept a
    # nonempty inline input merely because the advisory producer calls it skipped.
    return bool(re.fullmatch(r"\s*(?:inputs|workflow_dispatch)\s*:\s*(?:\{\}|\[\])\s*(?:#.*)?", line))


def validate_report(raw, patterns, sources):
    report = json_value(raw, MAX_REPORT, "invalid_report")
    if (not isinstance(report, dict) or set(report) != {"tool", "mode", "root", "findings", "summary", "coverage"}
            or report["tool"] != "ax-lint" or report["mode"] != "warn"
            or not isinstance(report["root"], str) or not isinstance(report["findings"], list)
            or not isinstance(report["summary"], dict) or set(report["summary"]) != {"findings", "mode"}
            or type(report["summary"]["findings"]) is not int
            or report["summary"]["findings"] != len(report["findings"])
            or report["summary"]["mode"] != "warn"
            or not isinstance(report["coverage"], dict) or set(report["coverage"]) != {"skipped", "unknown"}):
        refuse("invalid_report", "Restore the supported AX-lint warn-mode JSON schema and complete summary/coverage.")
    identities = set()
    seen = set()
    for finding in report["findings"]:
        if (not isinstance(finding, dict) or set(finding) != {"check", "rule_id", "path", "line", "message", "evidence", "remediation"}
                or not isinstance(finding["rule_id"], str) or finding["rule_id"] not in RULES
                or finding["check"] != RULES[finding["rule_id"]]
                or not line_number(finding["line"])
                or any(not isinstance(finding[k], str) or not finding[k].strip() for k in ("message", "remediation"))
                or finding["evidence"] is not None and not isinstance(finding["evidence"], str)):
            refuse("invalid_finding", "Use known AX-lint rules with complete located evidence and remediation.")
        path = safe_path(finding["path"])
        subject = "file"
        if finding["check"] == "dispatch-inputs":
            evidence = finding["evidence"]
            match = re.fullmatch(r"\s*([A-Za-z0-9_.-]+)\s*:\s*(?:#.*)?", evidence or "")
            if not match:
                refuse("ambiguous_identity", "Use supported named dispatch-entry evidence; do not grandfather anonymous occurrences.")
            subject = "input:" + match.group(1)
        identity = (finding["rule_id"], path, subject)
        if identity in seen:
            refuse("duplicate_identity", "Resolve duplicate diagnostic subjects; occurrence order or set-dedup is not a stable identity.")
        seen.add(identity)
        if matches(path, patterns):
            identities.add(identity)
    coverage = {"skipped": [], "unknown": []}
    for category in coverage:
        entries = report["coverage"][category]
        if not isinstance(entries, list):
            refuse("invalid_coverage", "Supply explicit skipped/unknown coverage lists, even when empty.")
        for entry in entries:
            if (not isinstance(entry, dict) or set(entry) != {"path", "line", "category", "reason"}
                    or entry["category"] != category or not line_number(entry["line"])
                    or not isinstance(entry["reason"], str) or not entry["reason"].strip()):
                refuse("invalid_coverage", "Supply located coverage entries with known categories and a nonempty reason.")
            path = safe_path(entry["path"])
            if matches(path, patterns):
                if category == "unknown" or not empty_skip(entry, sources):
                    refuse("unsupported_coverage", "Rewrite unsupported scoped inputs into the documented block mapping or explicit empty form.")
                coverage[category].append(entry)
    coverage["skipped"].sort(key=lambda entry: (entry["path"], entry["line"] or 0, entry["reason"]))
    return identities, coverage


def scan(root, patterns, sources):
    try:
        result = subprocess.run([sys.executable, "-I", "-B", str(PRODUCER), "--root", str(root),
                                 "--mode", "warn", "--json"], capture_output=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise Unmeasurable("producer_timeout", "Reduce scope or retry on a healthy runner.", True) from exc
    except OSError as exc:
        raise Unmeasurable("producer_unavailable", "Restore the pinned AX-lint producer and Python runtime.") from exc
    if result.returncode:
        refuse("producer_failed", "Inspect the pinned producer failure; do not treat missing output as a clean scan.")
    return validate_report(result.stdout, patterns, sources)


def identity_list(values):
    return [dict(zip(("rule_id", "path", "subject"), value)) for value in sorted(values)]


def check(repo, base, head):
    if not SHA.fullmatch(base) or not SHA.fullmatch(head):
        refuse("invalid_sha", "Provide full lowercase 40- or 64-character commit SHAs, not refs.")
    for sha in (base, head):
        if git(repo, "cat-file", "-t", sha).strip() != b"commit":
            refuse("invalid_commit", "Supply commit objects, not blobs, tags or trees.")
    base_tree = tree_at(repo, base)
    patterns = policy_at(repo, base_tree)
    report = {"schema": "diagnostic-delta.v1", "producer": "ax-lint", "base_sha": base,
              "head_sha": head, "policy_sha": base, "enforced": patterns is not None,
              "status": "not_configured" if patterns is None else "clean"}
    if patterns is None:
        return report
    with tempfile.TemporaryDirectory(prefix="ax-delta-") as tmp:
        base_root, head_root = Path(tmp) / "base", Path(tmp) / "head"
        base_root.mkdir()
        head_root.mkdir()
        base_source = snapshot(repo, base_tree, patterns, base_root)
        head_source = snapshot(repo, tree_at(repo, head), patterns, head_root)
        before, base_coverage = scan(base_root, patterns, base_source)
        after, head_coverage = scan(head_root, patterns, head_source)
    new, resolved = after - before, before - after
    report.update(status="finding" if new else "clean",
                  counts={"before": len(before), "after": len(after), "new": len(new), "resolved": len(resolved)},
                  new=identity_list(new), resolved=identity_list(resolved),
                  coverage={"base": base_coverage, "head": head_coverage})
    return report


def fingerprint(path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise Unmeasurable("invalid_arguments", "Provide --repo TREE --base FULL_SHA --head FULL_SHA; " + message)


def main(argv=None):
    parser = ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base", required=True, help="full trusted target commit SHA; supplies policy")
    parser.add_argument("--head", required=True, help="full candidate commit SHA")
    parser.add_argument("--json", action="store_true", help="explicit machine output (JSON is always emitted)")
    try:
        args = parser.parse_args(argv)
        report = check(args.repo, args.base, args.head)
    except (Unmeasurable, OSError) as exc:
        if isinstance(exc, OSError):
            exc = Unmeasurable("source_io", "Restore readable source and writable temporary snapshot storage.")
        report = {"schema": "diagnostic-delta.v1", "status": "unmeasurable", "code": exc.code,
                  "next_action": exc.next_action, "retryable": exc.retryable}
    report.update(checker_sha256=fingerprint(Path(__file__).resolve()),
                  producer_sha256=fingerprint(PRODUCER))
    print(json.dumps(report, sort_keys=True))
    return {"unmeasurable": 2, "finding": 1}.get(report["status"], 0)


if __name__ == "__main__":
    sys.exit(main())
