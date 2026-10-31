#!/usr/bin/env python3
"""Compare exact Git snapshots using the trusted AX-lint producer; stdout is JSON."""
import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time

POLICY = ".diagnostic-delta.json"
PRODUCER = Path(__file__).resolve().parents[2] / "templates/org-github/ax-lint/ax_lint.py"
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
MAX_REPORT = 1024 * 1024
MAX_SOURCE = 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_PATHS = 10000
# Documented outer bound for each Git collection subprocess; the pinned AX
# collector keeps its own 25-second inner deadline beneath this one.
GIT_TIMEOUT = 30
RULES = {
    "agents-map/missing": "agents-map",
    "agents-map/oversize": "agents-map",
    "script-surface/no-json-or-dry-run": "script-surface",
    "dispatch-inputs/untyped": "dispatch-inputs",
    "dispatch-inputs/choice-without-options": "dispatch-inputs",
}
# Workflow YAML is supported as the CI execution wrapper, never as a
# diagnostic input. Scope decisions refuse it on path selection, before any
# dispatch recognition: a refused input is unmeasurable, not clean.
WORKFLOW_SUFFIXES = (".yml", ".yaml")

WORKFLOW_SCOPE_ACTION = (
    "Narrow the policy include globs to shell/Python sources; workflow YAML "
    "is not a supported diagnostic input in this slice and must not be "
    "scoped as one.")
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
    # Collection is byte-bounded while Git runs (stdout: limit, stderr:
    # 64 KiB): the first excess byte kills and reaps the process instead of
    # spooling an unbounded listing to temporary storage before refusing it.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1",
               GIT_ALLOW_PROTOCOL="", GIT_TERMINAL_PROMPT="0",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    deadline = time.monotonic() + GIT_TIMEOUT
    try:
        process = subprocess.Popen(["git", "-C", str(repo), *args],
                                   stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, start_new_session=True)
    except OSError as exc:
        raise Unmeasurable("git_unavailable", "Install Git and select a readable repository.") from exc
    overflow = False
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        pending = memoryview(input) if input else None
        if input is not None and not input:
            process.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, ("stdout", limit))
            selector.register(process.stderr, selectors.EVENT_READ, ("stderr", 64 * 1024))
            if pending is not None:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, ("stdin", 0))
            while selector.get_map() and not overflow:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Unmeasurable("git_timeout", "Retry with complete history on a healthy runner.", True)
                for key, _ in selector.select(remaining):
                    name, cap = key.data
                    if name == "stdin":
                        try:
                            pending = pending[os.write(key.fd, pending[:65536]):]
                        except BlockingIOError:
                            continue
                        except OSError:
                            pending = memoryview(b"")
                        if not pending:
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                        continue
                    chunk = os.read(key.fd, min(65536, cap - len(buffers[name]) + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif len(buffers[name]) + len(chunk) > cap:
                        overflow = True
                    else:
                        buffers[name].extend(chunk)
        if overflow:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise Unmeasurable("git_timeout", "Retry with complete history on a healthy runner.", True) from exc
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    if overflow or returncode:
        refuse("git_failed", "Fetch the exact commits; keep the source within the documented snapshot limits.")
    return bytes(buffers["stdout"])


def tree_at(repo, sha):
    raw = git(repo, "ls-tree", "--full-tree", "-r", "-l", "-z", sha)
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
    parts = path.split("/")
    for pattern in patterns:
        positions = {0}
        for component in pattern.split("/"):
            if component == "**":
                # A whole ** component consumes zero or more directories.
                positions = set(range(min(positions), len(parts) + 1)) if positions else set()
            else:
                positions = {i + 1 for i in positions if i < len(parts)
                             and fnmatch.fnmatchcase(parts[i], component)}
        if len(parts) in positions:
            return True
    return False


def scope_ancestor(path, patterns):
    # Conservatively refuse symlink/submodule ancestors of wildcard scopes.
    # Only complete path components before the first wildcard component are
    # fixed: scripts/pkg-*/** fixes scripts/, so a symlink or gitlink that
    # replaces scripts/pkg-one is a possible scoped ancestor to refuse,
    # never resolved debt to certify.
    for pattern in patterns:
        parts = []
        for part in pattern.split("/"):
            if any(char in part for char in "*?["):
                break
            parts.append(part)
        prefix = "/".join(parts)
        if not prefix or path == prefix or prefix.startswith(path + "/") or path.startswith(prefix + "/"):
            return True
    return False


def scoped_workflows(tree, patterns):
    """Workflow/YAML paths the policy selects, whatever their tree mode.

    Every mode is refused, not only regular files: a symlink, gitlink or
    unusual mode that resolves to a workflow is still a workflow diagnostic
    input the policy asked to measure.
    """
    return sorted(path for path in tree
                  if path.endswith(WORKFLOW_SUFFIXES) and matches(path, patterns))


def snapshot(repo, tree, patterns, root):
    # Refuse workflow inputs before selecting or materializing anything: a
    # refused diagnostic input must never be silently filtered, reported
    # clean, or grandfathered because both snapshots share its syntax.
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
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation prevents case/normalization aliases from
            # overwriting a distinct Git identity on the runner filesystem.
            with target.open("xb") as stream:
                stream.write(raw)
            target.chmod(0o755 if selected[path][0] == b"100755" else 0o644)
        except OSError as exc:
            raise Unmeasurable("unsupported_projection", "Use readable temporary storage preserving distinct Git paths and modes.") from exc
    # Directory aliases can merge without a file collision (A/x, a/y).
    # Verify the round trip rather than assuming the filesystem preserves names.
    def unreadable(exc):
        raise Unmeasurable("unsupported_projection", "Use readable temporary storage preserving distinct Git paths and modes.") from exc
    projected = {str((Path(directory) / name).relative_to(root))
                 for directory, _, names in os.walk(root, onerror=unreadable)
                 for name in names}
    if projected != set(source):
        refuse("unsupported_projection", "Use temporary storage preserving every exact Git path, including case and Unicode.")
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
    return bool(re.fullmatch(r"\s*(['\"]?)(?:inputs|workflow_dispatch)\1\s*:\s*(?:\{\}|\[\])\s*(?:#.*)?", line))


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
    source_ancestors = {"/".join(parts[:i]) for source in sources
                        for parts in [source.split("/")] for i in range(1, len(parts))}
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
            if matches(path, patterns) or path in source_ancestors:
                if category == "unknown" or not empty_skip(entry, sources):
                    refuse("unsupported_coverage", "Rewrite unsupported scoped inputs or restore readable source files and ancestor directories; incomplete coverage cannot be measured.")
                coverage[category].append(entry)
    coverage["skipped"].sort(key=lambda entry: (entry["path"], entry["line"] or 0, entry["reason"]))
    return identities, coverage


def scan(root, patterns, sources):
    # The collector announces its session before spawning the producer. Both
    # stay in that group, so even a stopped collector cannot strand the producer
    # when subprocess.run enforces the outer deadline and kills only its child.
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    try:
        result = subprocess.run([sys.executable, "-I", "-B",
                                 str(Path(__file__).with_name("capture_ax_report.py")),
                                 str(PRODUCER), "--root", str(root), "--mode", "warn", "--json"],
                                capture_output=True, timeout=30, start_new_session=True,
                                pass_fds=(write_fd,),
                                env={**os.environ, "AX_COLLECTOR_SUPERVISOR_FD": str(write_fd)})
    except subprocess.TimeoutExpired as exc:
        raise Unmeasurable("producer_timeout", "Reduce scope or retry on a healthy runner.", True) from exc
    except OSError as exc:
        raise Unmeasurable("producer_unavailable", "Restore the pinned AX-lint producer and Python runtime.") from exc
    finally:
        os.close(write_fd)
        try:
            try:
                group = os.read(read_fd, 32)
            except BlockingIOError:
                group = b""
            if group.isdigit() and int(group) > 1:
                try:
                    os.killpg(int(group), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            os.close(read_fd)
    if result.returncode == 3:
        refuse("producer_output_limit", "Reduce scope: producer stdout is limited to 1 MiB and stderr to 64 KiB.")
    if result.returncode == 124:
        raise Unmeasurable("producer_timeout", "Reduce scope or retry on a healthy runner.", True)
    if result.returncode:
        refuse("producer_failed", "Inspect the pinned producer failure; do not treat missing output as a clean scan.")
    return validate_report(result.stdout, patterns, sources)


def identity_list(values):
    return [dict(zip(("rule_id", "path", "subject"), value)) for value in sorted(values)]


def check(repo, base, head):
    if not SHA.fullmatch(base) or not SHA.fullmatch(head):
        refuse("invalid_sha", "Provide full lowercase 40- or 64-character commit SHAs, not refs.")
    # A hex-looking name is not an immutable object: the length must match
    # this repository's object format and each value must be the exact full
    # object ID it resolves to, never a ref or an abbreviation.
    expected = {b"sha1": 40, b"sha256": 64}.get(git(repo, "rev-parse", "--show-object-format").strip())
    if expected is None or len(base) != expected or len(head) != expected:
        refuse("invalid_sha", "Provide full commit SHAs in this repository's object format, not refs or abbreviations.")
    for sha in (base, head):
        try:
            resolved = git(repo, "rev-parse", "--verify", sha + "^{commit}").strip()
        except Unmeasurable as exc:
            if exc.code != "git_failed":
                raise
            refuse("invalid_sha", "Provide exact full commit object IDs, not refs, abbreviations, blobs, tags or trees.")
        if resolved != sha.encode():
            refuse("invalid_sha", "Provide exact full commit object IDs, not refs, abbreviations, blobs, tags or trees.")
    base_tree = tree_at(repo, base)
    patterns = policy_at(repo, base_tree)
    report = {"schema": "diagnostic-delta.v1", "producer": "ax-lint", "base_sha": base,
              "head_sha": head, "policy_sha": base, "enforced": patterns is not None,
              "status": "not_configured" if patterns is None else "clean"}
    if patterns is None:
        return report
    head_tree = tree_at(repo, head)
    refused = {"base": scoped_workflows(base_tree, patterns),
               "head": scoped_workflows(head_tree, patterns)}
    if any(refused.values()):
        report.update(status="unmeasurable", code="unsupported_coverage",
                      next_action=WORKFLOW_SCOPE_ACTION, retryable=False,
                      refused=refused)
        return report
    with tempfile.TemporaryDirectory(prefix="ax-delta-") as tmp:
        base_root, head_root = Path(tmp) / "base", Path(tmp) / "head"
        base_root.mkdir()
        head_root.mkdir()
        base_source = snapshot(repo, base_tree, patterns, base_root)
        head_source = snapshot(repo, head_tree, patterns, head_root)
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
                  collector_sha256=fingerprint(Path(__file__).with_name("capture_ax_report.py")),
                  producer_sha256=fingerprint(PRODUCER))
    print(json.dumps(report, sort_keys=True))
    return {"unmeasurable": 2, "finding": 1}.get(report["status"], 0)


if __name__ == "__main__":
    sys.exit(main())
