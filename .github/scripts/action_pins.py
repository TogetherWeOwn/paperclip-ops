#!/usr/bin/env python3
"""Action pin gate: every `uses:` in workflows and local actions is SHA-pinned.

Supply-chain rule (case_10b7bbdf): a mutable tag ref lets a re-pointed tag
silently change what CI executes. Every `uses:` must be one of:

  * `./...`                          local action or reusable workflow
  * `docker://...@sha256:<64-hex>`   pinned container image
  * `owner/repo[/path]@<40-hex>`     third-party action or reusable workflow,
                                     SHA-pinned (e.g. `actions/checkout@<sha>`)

Anything else (tag ref, branch, short SHA, empty value) is a finding naming
`file:line`.

Usage: action_pins.py [--json] [workflow-file ...] (default:
.github/workflows/*.yml, *.yaml plus .github/actions/**/action.yml|action.yaml).
With `--json` a check run prints exactly one `action-pins.v1` object to stdout
(see docs/action-pins.md). Every run is offline and read-only: it is its own
dry-run. Runs inside the existing required `pr-lint` job, so adopting it adds
no new required check and needs no ruleset change. Parsing requires an explicitly
prepared PyYAML 6.0.2 interpreter; scans never prepare dependencies.
"""
import argparse
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "action-pins.v1"

PARSER_VERSION = "6.0.2"
MAX_SOURCE_BYTES = 4_194_304
MAX_COLLECTION_DEPTH = 128
MAX_PARSER_EVENTS = 100_000

SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
SHA64 = re.compile(r"^[0-9a-fA-F]{64}$")
# owner/repo, optional /path, @40-hex. Owner and repo allow word chars, dot,
# dash; path may nest with /.
PINNED_ACTION = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-fA-F]{40}$"
)


@dataclass
class Finding:
    file: str
    line: int
    value: str

    @property
    def message(self):
        return (
            f"{self.file}:{self.line}: unpinned action `{self.value or '(missing value)'}`. "
            "Pin third-party actions to a full commit SHA "
            "(`owner/repo@<40-hex>`), use `./...` for local actions, or "
            "`docker://...@sha256:<64-hex>` for containers."
        )


def strip_value(raw):
    """Extract the `uses:` value: drop trailing comment, flow closers, quotes."""
    # A `#` starts a comment (YAML values here never contain `#`).
    val = raw.split("#", 1)[0].strip()
    # Flow mapping closer: `- { uses: foo }` captures `foo }`.
    val = val.rstrip(",}").strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1].strip()
    return val


def is_pinned(value):
    if not value:
        return False
    if value.startswith("./"):
        return True
    if value.startswith("docker://"):
        ref, sep, digest = value.rpartition("@")
        if not sep:
            return False
        if not digest.startswith("sha256:"):
            return False
        return bool(SHA64.fullmatch(digest[len("sha256:"):]))
    return bool(PINNED_ACTION.fullmatch(value))


class ScanRefusal(Exception):
    """A known file could not be completely parsed and inspected."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _display_path(path):
    """Recover valid UTF-8 argv bytes; retain invalid bytes reversibly."""
    return re.sub(
        r"[\udc80-\udcff]+",
        lambda match: match.group().encode("utf-8", errors="surrogateescape").decode(
            "utf-8", errors="surrogateescape"),
        str(path),
    )


def _yaml_parser():
    try:
        import yaml
    except ImportError as exc:
        raise ScanRefusal("input-parser-unavailable",
                          "prepare the isolated PyYAML 6.0.2 environment") from exc
    if getattr(yaml, "__version__", None) != PARSER_VERSION:
        raise ScanRefusal("input-parser-unavailable",
                          "the isolated parser must be PyYAML 6.0.2")
    return yaml


def _compose_documents(text, yaml):
    """Validate event bounds/unsupported graph features before composition."""
    depth = 0
    events = yaml.parse(text, Loader=yaml.SafeLoader)
    try:
        for count, event in enumerate(events, 1):
            if count > MAX_PARSER_EVENTS:
                raise ScanRefusal("input-resource-limit", "parser event limit exceeded")
            if isinstance(event, yaml.events.AliasEvent):
                raise ScanRefusal("input-unsupported-yaml", "YAML aliases are unsupported")
            if isinstance(event, yaml.events.CollectionStartEvent):
                depth += 1
                if depth > MAX_COLLECTION_DEPTH:
                    raise ScanRefusal("input-resource-limit", "YAML collection depth exceeded")
            elif isinstance(event, yaml.events.CollectionEndEvent):
                depth -= 1
        # Composition retains mapping pairs and source marks, without calling
        # constructors. The prior pass rules out aliases and caps the graph.
        documents = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    except (yaml.YAMLError, ValueError, OverflowError) as exc:
        raise ScanRefusal("input-invalid-yaml", f"invalid YAML ({exc})") from exc
    except RecursionError as exc:
        raise ScanRefusal("input-resource-limit", "parser recursion limit reached") from exc
    finally:
        events.close()
    return documents


def _validate_nodes(documents, yaml):
    scalar_tags = {"tag:yaml.org,2002:" + tag for tag in
                   ("str", "null", "bool", "int", "float", "timestamp", "binary")}
    stack = [node for node in documents if node is not None]
    while stack:
        node = stack.pop()
        if isinstance(node, yaml.nodes.ScalarNode):
            if node.tag not in scalar_tags:
                raise ScanRefusal("input-unsupported-yaml", "unsupported scalar tag or merge key")
        elif isinstance(node, yaml.nodes.MappingNode):
            if node.tag != "tag:yaml.org,2002:map":
                raise ScanRefusal("input-unsupported-yaml", "unsupported mapping tag")
            for key, value in node.value:
                if not isinstance(key, yaml.nodes.ScalarNode):
                    raise ScanRefusal("input-unsupported-yaml", "complex mapping keys are unsupported")
                stack.extend((key, value))
        elif isinstance(node, yaml.nodes.SequenceNode):
            if node.tag != "tag:yaml.org,2002:seq":
                raise ScanRefusal("input-unsupported-yaml", "unsupported sequence tag")
            stack.extend(node.value)
        else:
            raise ScanRefusal("input-unsupported-yaml", "unsupported YAML node")


def _invocation_values(documents, yaml):
    mapping, sequence, scalar = (yaml.nodes.MappingNode, yaml.nodes.SequenceNode,
                                 yaml.nodes.ScalarNode)
    string_tag = "tag:yaml.org,2002:str"
    found = []

    def pairs(node):
        if not isinstance(node, mapping):
            raise ScanRefusal("input-unsupported-yaml", "expected an invocation mapping")
        return node.value

    def values(node, name):
        result = []
        for key, value in pairs(node):
            if key.value == name:
                if key.tag != string_tag:
                    raise ScanRefusal("input-unsupported-yaml", "invocation keys must be strings")
                result.append((key, value))
        return result

    def invocation(node):
        for key, value in values(node, "uses"):
            if not isinstance(value, scalar):
                raise ScanRefusal("input-unsupported-yaml", "uses values must be scalar strings")
            if value.tag == "tag:yaml.org,2002:null" and value.value == "":
                ref = ""  # Preserve the existing missing-value finding.
            elif value.tag == string_tag:
                ref = value.value
            else:
                raise ScanRefusal("input-unsupported-yaml", "uses values must be strings")
            found.append((key.start_mark.line + 1, ref))

    def steps(node, fragment=False):
        if fragment and isinstance(node, mapping):
            invocation(node)
        elif isinstance(node, sequence):
            for step in node.value:
                if (isinstance(step, scalar) and step.tag == "tag:yaml.org,2002:null"
                        and step.value == ""):
                    continue  # Empty sequence entries do not contain invocations.
                invocation(step)
        else:
            raise ScanRefusal("input-unsupported-yaml", "expected a step sequence")

    for root in documents:
        if root is None:
            continue
        if isinstance(root, sequence):
            steps(root)
        elif isinstance(root, mapping):
            jobs, runs = values(root, "jobs"), values(root, "runs")
            if jobs or runs:
                for _, job_map in jobs:
                    for _, job in pairs(job_map):
                        invocation(job)
                        for _, step_list in values(job, "steps"):
                            steps(step_list)
                for _, action in runs:
                    for _, step_list in values(action, "steps"):
                        steps(step_list)
            else:
                invocation(root)
                for _, step_list in values(root, "steps"):
                    steps(step_list, fragment=True)
    return found


def lint_text(path, text):
    if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ScanRefusal("input-resource-limit", "source byte limit exceeded")
    yaml = _yaml_parser()
    documents = _compose_documents(text, yaml)
    _validate_nodes(documents, yaml)
    values = _invocation_values(documents, yaml)
    return [Finding(_display_path(path), line, value) for line, value in values
            if not is_pinned(value)]


def lint_file(path):
    with open(path, "rb") as source:
        raw = source.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ScanRefusal("input-resource-limit", "source byte limit exceeded")
    return lint_text(path, raw.decode("utf-8"))


def _escape(text):
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text):
    return _escape(text).replace(",", "%2C").replace(":", "%3A")


def _discover_files():
    """Return candidate paths and observable directory-enumeration errors.

    Missing optional roots are normal. Other failed enumerations are scan
    errors, not refused files: their unknown children cannot be counted.
    """
    found, errors = set(), []

    pending = [
        (os.path.join(".github", "workflows"), False, True, frozenset()),
        (os.path.join(".github", "actions"), True, True, frozenset()),
    ]
    while pending:
        directory, recursive, optional, ancestors = pending.pop()
        try:
            identity = os.stat(directory)
            identity = (identity.st_dev, identity.st_ino)
            if identity in ancestors:
                continue  # Do not follow a directory symlink cycle.
            with os.scandir(directory) as entries:
                entries = sorted(entries, key=lambda entry: entry.name)
        except FileNotFoundError as exc:
            if not optional:
                errors.append(_input_error(
                    "input-unreadable", directory,
                    f"{directory}:1: cannot enumerate directory ({exc})"))
            continue
        except OSError as exc:
            errors.append(_input_error(
                "input-unreadable", directory,
                f"{directory}:1: cannot enumerate directory ({exc})"))
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue  # Preserve glob's default hidden-path exclusion.
            candidate = (entry.name in ("action.yml", "action.yaml") if recursive
                         else entry.name.endswith((".yml", ".yaml")))
            if candidate:
                found.add(entry.path)
            if recursive:
                try:
                    is_directory = entry.is_dir()
                except OSError as exc:
                    errors.append(_input_error(
                        "input-unreadable", entry.path,
                        f"{entry.path}:1: cannot inspect directory ({exc})"))
                    continue
                if is_directory:
                    pending.append((entry.path, True, False, ancestors | {identity}))

    return sorted(found), sorted(errors, key=lambda error: (error.path, error.code))


def default_files():
    return _discover_files()[0]


@dataclass
class InputError:
    code: str
    path: object  # str, or None for the scan-level no-inputs error
    message: str
    next_action: str
    retryable: bool = False


_ERROR_ACTIONS = {
    "input-missing": "fix_input_path",
    "input-unreadable": "fix_input_permissions",
    "input-invalid-encoding": "fix_input_encoding",
    "no-inputs-discovered": "provide_input_files",
    "input-parser-unavailable": "prepare_parser_dependency",
    "input-invalid-yaml": "fix_input_yaml",
    "input-unsupported-yaml": "simplify_input_yaml",
    "input-resource-limit": "reduce_input_complexity",
}

_NO_INPUTS_MESSAGE = (
    "no workflow files found "
    "(expected .github/workflows/*.yml|*.yaml or "
    ".github/actions/**/action.y*ml)"
)


def _input_error(code, path, message):
    return InputError(code, None if path is None else _display_path(path),
                      _display_path(message), _ERROR_ACTIONS[code])


def _refuse_special_file(path):
    """Refuse existing inputs that are neither regular files nor directories.

    Reading a FIFO with no writer blocks the scan with zero output; sockets
    and devices are likewise never workflow sources. Refuse them before any
    read attempt. Directories fall through to the normal read path so their
    refusal keeps its established message. Anything `stat` cannot inspect
    (missing, permission-denied) also falls through to the existing handlers.
    """
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return None
    if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
        return None
    return _input_error(
        "input-unreadable", str(path),
        f"{path}:1: unreadable file (not a regular file)")


def scan_files(files):
    """Check each file; return (findings, input_errors, checked_count).

    A file that cannot be read is refused (one InputError) and never counted
    as checked, so missing evidence cannot read as a clean scan. Findings
    from files that were read survive other files' failures.
    """
    findings = []
    errors = []
    checked = 0
    for path in files:
        refusal = _refuse_special_file(path)
        if refusal is not None:
            errors.append(refusal)
            continue
        try:
            found = lint_file(path)
        except ScanRefusal as exc:
            errors.append(_input_error(exc.code, str(path), f"{path}:1: {exc}"))
        except FileNotFoundError:
            errors.append(
                _input_error("input-missing", str(path), f"missing file {_display_path(path)}"))
        except UnicodeDecodeError as exc:
            errors.append(_input_error(
                "input-invalid-encoding", str(path),
                f"{path}:1: invalid encoding ({exc})"))
        except (OSError, ValueError) as exc:
            errors.append(_input_error(
                "input-unreadable", str(path),
                f"{path}:1: unreadable file ({exc})"))
        else:
            checked += 1
            findings.extend(found)
    return findings, errors, checked


def _finding_entry(finding):
    return {
        "code": "unpinned-action",
        "path": finding.file,
        "line": finding.line,
        "value": finding.value,
        "message": finding.message,
        "retryable": False,
        "next_action": "pin_to_full_sha_or_digest",
    }


def _error_entry(error):
    return {
        "code": error.code,
        "path": error.path,
        "message": error.message,
        "retryable": error.retryable,
        "next_action": error.next_action,
    }


def build_report(requested, findings, errors, checked, refused):
    finding_entries = sorted(
        (_finding_entry(f) for f in findings),
        key=lambda e: (e["path"], e["line"], e["code"], e["value"]),
    )
    error_entries = sorted(
        (_error_entry(e) for e in errors),
        key=lambda e: (e["path"] or "", e["code"]),
    )
    scan_complete = requested > 0 and checked == requested and not error_entries
    ok = scan_complete and not finding_entries and not error_entries
    if ok:
        next_action = "none"
    elif error_entries:
        next_action = error_entries[0]["next_action"]
    else:
        next_action = finding_entries[0]["next_action"]
    return {
        "schema": SCHEMA,
        "ok": ok,
        "scan_complete": scan_complete,
        "requested": requested,
        "checked": checked,
        "refused": refused,
        "findings": finding_entries,
        "errors": error_entries,
        "retryable": False,
        "next_action": next_action,
    }


def _ensure_encodable_stdout():
    """Harden the text emitter against ASCII locales (best-effort).

    Under `LC_ALL=C` stdout encodes as ASCII, so printing a finding whose
    value or path holds non-ASCII text raises `UnicodeEncodeError` and the
    run ends in a traceback instead of a verdict. Prefer UTF-8 output with
    a backslash fallback; the annotation content is unchanged, only the
    transport encoding is hardened. Streams without `reconfigure`
    (captured, closed, unsupported) keep their existing behavior.
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="backslashreplace")
    except (ValueError, OSError):
        pass


def _print_text(report, errors):
    _ensure_encodable_stdout()
    for error in errors:
        if error.path is None:
            print(f"::error ::{error.message}")
        else:
            print(f"::error file={_escape_property(error.path)},line=1::{_escape(error.message)}")
    for entry in report["findings"]:
        print(
            f"::error file={_escape_property(entry['path'])},line={entry['line']}::"
            f"{_escape(entry['message'])}"
        )
    if report["ok"]:
        print(f"Action pins OK ({report['checked']} workflow(s) checked)")


def _parser(prog):
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Check that every `uses:` in workflows and local actions is "
            "SHA-pinned (`owner/repo[/path]@<40-hex>`, `./...` local actions, or "
            "`docker://...@sha256:<64-hex>`). Offline and read-only."
        ),
        epilog=(
            "With --json a check run prints exactly one action-pins.v1 JSON "
            "object to stdout. Check exits: 0 clean, 1 findings or refused "
            "input/discovery. Counts are unique file paths; directory errors "
            "are additional and force an incomplete scan. Usage errors exit 2 "
            "with text on stderr and are never "
            "JSON; --help prints this text and exits 0 even with --json. "
            "Parsing requires isolated PyYAML 6.0.2; see docs/action-pins.md."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="print one action-pins.v1 JSON verdict object to stdout",
    )
    parser.add_argument(
        "paths", nargs="*", metavar="PATH",
        help=(
            "workflow/action files to check (default: "
            ".github/workflows/*.yml|*.yaml and "
            ".github/actions/**/action.yml|action.yaml)"
        ),
    )
    return parser


def main(argv=None):
    _ensure_encodable_stdout()
    argv = list(sys.argv if argv is None else argv)
    prog = os.path.basename(argv[0]) if argv else "action_pins.py"
    args = _parser(prog).parse_args(argv[1:])
    if args.paths:
        files = sorted(set(args.paths))
        findings, errors, checked = scan_files(files)
        requested, refused = len(files), len(errors)
    else:
        files, discovery_errors = _discover_files()
        requested = len(files)
        findings, errors, checked = scan_files(files)
        refused = len(errors)
        errors.extend(discovery_errors)
        if not files and not errors:
            errors = [_input_error(
                "no-inputs-discovered", None, _NO_INPUTS_MESSAGE)]
    report = build_report(requested, findings, errors, checked, refused)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        _print_text(report, errors)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
