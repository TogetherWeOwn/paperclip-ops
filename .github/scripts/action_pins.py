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

Usage: action_pins.py [workflow-file ...] (default: .github/workflows/*.yml,
*.yaml plus .github/actions/**/action.yml|action.yaml).
Runs inside the existing required `pr-lint` job, so adopting it adds no new
required check and needs no ruleset change. Stdlib only.
"""
import glob
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

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
            f"{self.file}:{self.line}: unpinned action `{self.value}`. "
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
            out.append(Finding(str(path), i, "(missing value)"))
            continue
        if not is_pinned(value):
            out.append(Finding(str(path), i, value))
    return out


def lint_file(path):
    return lint_text(path, Path(path).read_text())


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


def main(argv=None):
    explicit = list(argv[1:] if argv is not None else sys.argv[1:])
    if explicit:
        files = sorted(set(explicit))
        missing = [p for p in files if not os.path.exists(p)]
        for p in missing:
            print(f"::error file={p},line=1::{_escape(f'missing file {p}')}")
        findings = []
        for path in files:
            if not os.path.exists(path):
                continue
            try:
                findings.extend(lint_file(path))
            except (OSError, UnicodeDecodeError) as exc:
                msg = f"{path}:1: unreadable file ({exc})"
                print(f"::error file={path},line=1::{_escape(msg)}")
                findings.append(Finding(str(path), 1, "(unreadable)"))
        for f in findings:
            print(f"::error file={f.file},line={f.line}::{_escape(f.message)}")
        if missing or findings:
            return 1
        print(f"Action pins OK ({len(files)} workflow(s) checked)")
        return 0
    files = default_files()
    if not files:
        print(
            "::error ::no workflow files found "
            "(expected .github/workflows/*.yml|*.yaml or "
            ".github/actions/**/action.y*ml)"
        )
        return 1
    findings = []
    for path in files:
        findings.extend(lint_file(path))
    for f in findings:
        print(f"::error file={f.file},line={f.line}::{_escape(f.message)}")
    if findings:
        return 1
    print(f"Action pins OK ({len(files)} workflow(s) checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
