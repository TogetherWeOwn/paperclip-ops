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
no new required check and needs no ruleset change. Stdlib only.
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

# Quoted scalars are consumed whole, so flow punctuation in shell strings
# cannot become mapping keys. This is extraction, not a general YAML loader.
_QUOTED = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:''|[^'])*' ''', re.X)
_BLOCK_SCALAR = re.compile(r"[|>](?:[1-9][+-]?|[+-][1-9]?)?\s*(?:#.*)?$")
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
        return bool(SHA64.match(digest[len("sha256:"):]))
    return bool(PINNED_ACTION.match(value))


def _scalar_end(text, start, *, flow=False, key=False):
    quoted = _QUOTED.match(text, start)
    if quoted:
        return quoted.end()
    pos = start
    while pos < len(text) and text[pos] not in "\r\n#":
        char = text[pos]
        if flow and char in ",[]{}":
            break
        if key and char == ":" and (
                pos + 1 == len(text) or text[pos + 1].isspace()
                or (flow and text[pos + 1] in ",[]{}")):
            break
        pos += 1
    return pos


def _key_text(raw):
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            return json.loads(raw)  # Includes Unicode escapes in quoted keys.
        except ValueError:
            return raw
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    return raw


def _skip_space(text, pos):
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
        elif text[pos] == "#":
            end = text.find("\n", pos)
            pos = len(text) if end < 0 else end + 1
        else:
            break
    return pos


def _flow_uses(text, start):
    """Extract mapping keys from a flow collection without loading a YAML AST."""
    mapping = text[start] == "{"
    close = "}" if mapping else "]"
    found, pos = [], start + 1
    while True:
        pos = _skip_space(text, pos)
        if pos >= len(text):
            return found, pos
        if text[pos] == close:
            return found, pos + 1
        if text[pos] == ",":
            pos += 1
            continue
        key, key_start = None, pos
        if mapping:
            if text[pos] == "?" and text[pos + 1:pos + 2].isspace():
                pos = _skip_space(text, pos + 1)
                key_start = pos
            end = _scalar_end(text, pos, flow=True, key=True)
            key = _key_text(text[pos:end])
            pos = _skip_space(text, end)
            if pos >= len(text) or text[pos] != ":":
                return found, pos
            pos = _skip_space(text, pos + 1)
        value_start = pos
        if pos < len(text) and text[pos] in "[{":
            nested, pos = _flow_uses(text, pos)
            found.extend(nested)
        else:
            pos = _scalar_end(text, pos, flow=True)
        if key == "uses":
            found.append((text.count("\n", 0, key_start) + 1,
                          strip_value(text[value_start:pos])))
        if pos == value_start and (pos >= len(text) or text[pos] not in (",", close)):
            return found, pos  # Empty values before delimiters are valid.


def _uses_values(text):
    """Support block/flow scalar keys, explicit keys and quoted key escapes.

    Skip scalar bodies rather than searching their contents for `uses:`.
    Anchors, aliases, tags and complex keys are not resolved by this checker.
    """
    found, offset, consumed = [], 0, 0
    block_indent, explicit = None, None
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        start = offset
        offset += len(line)
        if start < consumed:
            continue
        content = line.lstrip()
        indent = len(line) - len(content)
        if not content.strip() or content.startswith("#"):
            continue
        if block_indent is not None and indent > block_indent:
            continue
        block_indent = None
        pos = start + indent
        if text[pos] == "-" and text[pos + 1:pos + 2].isspace():
            pos += 1
            while pos < offset and text[pos] in " \t":
                pos += 1
            if pos >= offset or text[pos] in "\r\n":
                continue  # An empty sequence item must not consume the next line.
        mapping_indent = pos - start
        key_line = number
        if explicit and text[pos] == ":":
            key, key_line = explicit
            explicit = None
            pos += 1
        else:
            if explicit and explicit[0] == "uses":
                found.append((explicit[1], ""))
            explicit = None
            if text[pos] in "[{":
                nested, consumed = _flow_uses(text, pos)
                found.extend(nested)
                continue
            is_explicit = text[pos:pos + 2] == "? "
            if is_explicit:
                pos += 2
            end = _scalar_end(text, pos, key=True)
            key = _key_text(text[pos:end])
            pos = end
            while pos < offset and text[pos] in " \t":
                pos += 1
            if pos >= offset or text[pos] != ":":
                if is_explicit:
                    explicit = (key, number)
                continue
            pos += 1
        while pos < offset and text[pos] in " \t":
            pos += 1
        if pos < offset and text[pos] in "[{":
            nested, consumed = _flow_uses(text, pos)
            found.extend(nested)
            end = consumed
        else:
            end = _scalar_end(text, pos)
            if _BLOCK_SCALAR.fullmatch(text[pos:offset].strip()):
                block_indent = mapping_indent
        if key == "uses":
            found.append((key_line, strip_value(text[pos:end])))
    if explicit and explicit[0] == "uses":
        found.append((explicit[1], ""))
    return found


def lint_text(path, text):
    return [Finding(str(path), line, value) for line, value in _uses_values(text)
            if not is_pinned(value)]


def lint_file(path):
    return lint_text(path, Path(path).read_text(encoding="utf-8"))


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

    def visit(directory, recursive=False, optional=False, ancestors=frozenset()):
        try:
            identity = os.stat(directory)
            identity = (identity.st_dev, identity.st_ino)
            if identity in ancestors:
                return  # Do not follow a directory symlink cycle.
            with os.scandir(directory) as entries:
                entries = sorted(entries, key=lambda entry: entry.name)
        except FileNotFoundError as exc:
            if not optional:
                errors.append(_input_error(
                    "input-unreadable", directory,
                    f"{directory}:1: cannot enumerate directory ({exc})"))
            return
        except OSError as exc:
            errors.append(_input_error(
                "input-unreadable", directory,
                f"{directory}:1: cannot enumerate directory ({exc})"))
            return
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
                    visit(entry.path, True, ancestors=ancestors | {identity})

    visit(os.path.join(".github", "workflows"), optional=True)
    visit(os.path.join(".github", "actions"), recursive=True, optional=True)
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
}

_NO_INPUTS_MESSAGE = (
    "no workflow files found "
    "(expected .github/workflows/*.yml|*.yaml or "
    ".github/actions/**/action.y*ml)"
)


def _input_error(code, path, message):
    return InputError(code, path, message, _ERROR_ACTIONS[code])


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
        except FileNotFoundError:
            errors.append(
                _input_error("input-missing", str(path), f"missing file {path}"))
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
            "JSON; --help prints this text and exits 0 even with --json."
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
