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
import glob
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "action-pins.v1"

# `uses` as a YAML mapping key: at line start (after `-`/indent), or after
# `{`/`,` in a flow mapping. Covers `- uses:`, `uses:`, `- uses :`,
# `"uses":`, and `- { uses: ... }`. Deliberately not "anywhere": a `uses:`
# inside a `run:` script value must not flag.
USES = re.compile(r'(?:^|[{,\-])\s*["\']?uses["\']?\s*:(?:\s*(?P<val>.+?))?\s*$')
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


def lint_text(path, text):
    out = []
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        m = USES.search(line)
        if not m:
            continue
        value = strip_value(m.group("val") or "")
        if not value:
            out.append(Finding(str(path), i, ""))
            continue
        if not is_pinned(value):
            out.append(Finding(str(path), i, value))
    return out


def lint_file(path):
    return lint_text(path, Path(path).read_text(encoding="utf-8"))


def _escape(text):
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def default_files():
    pats = [
        os.path.join(".github", "workflows", "*.yml"),
        os.path.join(".github", "workflows", "*.yaml"),
        os.path.join(".github", "actions", "**", "action.yml"),
        os.path.join(".github", "actions", "**", "action.yaml"),
    ]
    found = []
    for pat in pats:
        found.extend(glob.glob(pat, recursive=True))
    return sorted(set(found))


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
    scan_complete = requested > 0 and checked == requested
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
            print(f"::error file={error.path},line=1::{_escape(error.message)}")
    for entry in report["findings"]:
        print(
            f"::error file={entry['path']},line={entry['line']}::"
            f"{_escape(entry['message'])}"
        )
    if report["ok"]:
        print(f"Action pins OK ({report['checked']} workflow(s) checked)")


def _parser(prog):
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Check that every `uses:` in workflows and local actions is "
            "SHA-pinned (`owner/repo@<40-hex>`, `./...` local actions, or "
            "`docker://...@sha256:<64-hex>`). Offline and read-only."
        ),
        epilog=(
            "With --json a check run prints exactly one action-pins.v1 JSON "
            "object to stdout. Check exits: 0 clean, 1 findings or refused "
            "input. Usage errors exit 2 with text on stderr and are never "
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
        files = default_files()
        requested = len(files)
        if files:
            findings, errors, checked = scan_files(files)
            refused = len(errors)
        else:
            findings, checked, refused = [], 0, 0
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
