#!/usr/bin/env python3
"""Re-validate every ADR citation for case_01fa2cdd against the installed Paperclip tree.

The ADR (docs/native-ssh-os-authority-split.md) names exact `file:line` seams. Line
numbers rot silently: a citation that drifts still *reads* authoritative, and a seam
that was never deployed reads identical to one that was. Both failures previously
reached a board comment as fact.

Each citation is therefore keyed on ANCHOR TEXT, not on a line number. The gate
reports three distinct outcomes, and only the first is clean:

  present   anchor found; the recorded line is correct
  drifted   anchor found at a DIFFERENT line; the ADR's number is stale
  absent    anchor not found in the installed tree at all

`absent` is the load-bearing case. A seam accepted on a branch but never deployed is
absent here, and calling that "protected" is the exact error this gate exists to stop.

Read-only. Never prints file contents beyond the matched anchor line.

Exit codes:
  0  every citation present at its recorded line
  1  one or more citations drifted or absent
  2  usage or audit error
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

# (key, path, recorded_line, anchor_substring, why_it_matters)
#
# recorded_line is the last-verified live line (re-pinned 2026-10-22 to the
# tog.22 tree, commit 8b8dcb7d5444719b6e269a875d773ce3609a28fb; the ADR's
# printed §1.0-§1.1 numbers describe the 2026-08-31 tree — see the §1.2/§1.3
# addenda). anchor is a substring unique within the file. Uniqueness is
# enforced at runtime: a non-unique anchor is an audit error,
# not a silent first-match, because a first-match resolver reads the wrong site.
CITATIONS: tuple[tuple[str, str, int, str, str], ...] = (
    (
        "spawn_target_resolver",
        "/app/packages/adapter-utils/src/server-utils.ts",
        3741,
        "async function resolveSpawnTarget(",
        "constructs the ssh spawn target; the native-SSH carrier",
    ),
    (
        "ssh_resolved_no_guard",
        "/app/packages/adapter-utils/src/server-utils.ts",
        3787,
        'const sshResolved = await resolveCommandPath("ssh", process.cwd(), env)',
        "ssh is resolved here with no assigned-model/authority throw preceding it",
    ),
    (
        "child_spawn_site",
        "/app/packages/adapter-utils/src/server-utils.ts",
        4934,
        "const child = spawn(target.command, target.args, {",
        "the shared child-process spawn seam",
    ),
    (
        "run_child_process",
        "/app/packages/adapter-utils/src/server-utils.ts",
        4833,
        "function runChildProcess",
        "narrow seam; insufficient alone, ACP and in-process adapters bypass it",
    ),
    (
        "ssh_argv_builder",
        "/app/packages/adapter-utils/src/ssh.ts",
        1541,
        "export async function buildSshSpawnTarget(",
        "builds remote command/argv/cwd/env",
    ),
    (
        "remote_runtime_staging",
        "/app/packages/adapter-utils/src/remote-managed-runtime.ts",
        138,
        'path.posix.join(workspaceRemoteDir, ".paperclip-runtime", input.adapterKey)',
        "runtime staged into an agent-writable remote tree; non-authoritative",
    ),
    (
        "adapter_execute_seam",
        "/app/server/src/services/heartbeat.ts",
        25879,
        "() => adapter.execute({",
        "the complete per-run executor boundary the ADR selects",
    ),
    (
        "plugin_dynamic_import",
        "/app/server/src/adapters/plugin-loader.ts",
        195,
        "const mod = await import(pathToFileURL(modulePath).href)",
        "external adapter code loaded into the server process",
    ),
    (
        "acpx_runtime_options",
        "/app/packages/adapter-utils/src/acpx-engine/execute.ts",
        4256,
        "const runtimeOptions: PaperclipAcpRuntimeOptions = {",
        "ACP builds its runtime outside runChildProcess()",
    ),
    (
        "decision_signing_uid_check",
        "/app/server/src/services/decision-signing.ts",
        17,
        "stats.uid !== currentUserId",
        "signing key is bound to the CURRENT process uid, which is the agent uid",
    ),
)

# Symbols the case_e4054a66 acceptance comment cited as deployed protection. The seam is
# now live dormant in /app (tog.22), so each must be PRESENT in the installed tree
# for the gate's "deployed" finding to hold. If one is missing, the finding is stale
# and the ADR must be re-reviewed, so absence fails.
#
# assertAssignedModel + launchAuthorization dropped per the 2026-09-01 finding
# (identifiers never existed in the accepted tree).
PRESENT_EXPECTED: tuple[tuple[str, str], ...] = (
    ("hostExecutor", "case_e4054a66 host-executor seam"),
    ("HostExecutor", "case_e4054a66 host-executor seam (type)"),
    ("authorizeLaunch", "signer-issued launch authorization"),
)

PRESENT_FILE = "/app/packages/adapter-utils/src/server-utils.ts"
ABSENT_FILE = "/app/packages/adapter-utils/src/ssh.ts"

# HostExecutor is the historical seam label, not a standalone type in tog.22.
# Match its two deployed identifiers explicitly rather than arbitrary substrings.
SYMBOL_IDENTIFIERS = {
    "HostExecutor": {"HostExecutor", "NativeSshHostExecutorTarget", "getActiveNativeSshHostExecutor"},
}

# Skip comments and quoted text before matching complete identifiers. Template
# *text* is skipped but `${...}` interpolations are executable code, so their
# identifiers are counted. Regex literals are skipped only in the required file,
# where counting a literal as deployed code would be a false present; the
# forbidden file uses a conservative count that never skips `/.../` (a seam in
# a regex there fails closed rather than hiding). Division is distinguished
# from a regex by the previous significant token. Line comments end at CR, LF,
# U+2028 and U+2029 per the TypeScript scanner. This is a lexical citation
# check, not a TypeScript parser or proof that an authorization path executes.
IDENT_RE = re.compile(r"[$\w]+")

LINE_TERMINATORS = "\r\n\u2028\u2029"

# Value-like words: an expression just ended, so `/` is division.
VALUE_KEYWORDS = frozenset({"this", "true", "false", "null", "super"})

# Keywords in expression-expected position: `/` opens a regex literal.
REGEX_ALLOWED_KEYWORDS = frozenset(
    {
        "return", "typeof", "instanceof", "in", "of", "new", "delete",
        "void", "throw", "case", "do", "else", "yield", "await",
        "function", "if", "for", "while", "with", "catch", "switch",
        "var", "let", "const", "class", "extends", "import", "export",
        "default", "try", "finally", "break", "continue", "debugger",
        "as", "implements", "interface", "package", "private",
        "protected", "public", "static", "enum", "declare",
    }
)


def find_anchor(path: Path, anchor: str) -> tuple[str, int | None, str]:
    """Return (state, line_number, detail). State is present/absent/error."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return "absent", None, "file_not_found"
    except OSError as exc:
        return "error", None, f"unreadable: {exc.strerror}"

    hits = [i for i, line in enumerate(text.splitlines(), start=1) if anchor in line]
    if not hits:
        return "absent", None, "anchor_not_found"
    if len(hits) > 1:
        # A first-match resolver would answer with the wrong site. Refuse instead.
        return "error", None, f"anchor_not_unique: {len(hits)} matches at {hits}"
    return "present", hits[0], "ok"


def _try_parse_regex(text: str, pos: int) -> int | None:
    """Return the end of a regex literal starting at pos, or None if invalid.

    pos must point at `/` whose next char is neither `/` nor `*` (comments are
    handled before this is called). An unescaped line terminator or a missing
    closing slash means this is division, not a regex.
    """
    n = len(text)
    i = pos + 1
    in_class = False
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch in LINE_TERMINATORS:
            return None
        if ch == "[" and not in_class:
            in_class = True
            i += 1
            continue
        if ch == "]" and in_class:
            in_class = False
            i += 1
            continue
        if ch == "/" and not in_class:
            i += 1
            while i < n and "a" <= text[i] <= "z":
                i += 1
            return i
        i += 1
    return None


def _scan_template(
    text: str, start: int, identifiers: set[str], conservative: bool
) -> tuple[int, int]:
    """Scan the template opening at start; return (end_pos, interpolation_hits).

    Template text is skipped; each `${...}` interpolation is scanned as code so
    executable seam references inside interpolations are counted. Handles
    arbitrary nesting via mutual recursion with _scan_js.
    """
    n = len(text)
    pos = start + 1  # past the opening backtick
    count = 0
    while pos < n:
        ch = text[pos]
        if ch == "\\":
            pos += 2
            continue
        if ch == "`":
            return pos + 1, count
        if text.startswith("${", pos):
            pos, inner = _scan_js(
                text, pos + 2, identifiers, conservative, in_interpolation=True
            )
            count += inner
            continue
        pos += 1
    return n, count


def _scan_js(
    text: str,
    start: int,
    identifiers: set[str],
    conservative: bool,
    in_interpolation: bool,
) -> tuple[int, int]:
    """Scan JS/TS from start; return (end_pos, identifier_hits).

    When in_interpolation is true, stop after the matching `}` of the `${`
    that opened this call (brace depth tracked from 1). Otherwise scan to the
    end of text. prev tracks the previous significant token: 'operand' means
    an expression just ended so `/` is division; anything else means `/` may
    open a regex. conservative=True (forbidden file) never skips `/.../` so a
    seam there cannot hide behind a misclassified division.
    """
    n = len(text)
    pos = start
    count = 0
    prev: str | None = None  # None | 'operand' | 'operator'
    depth = 1 if in_interpolation else 0
    while pos < n:
        ch = text[pos]
        if ch in " \t\v\f" or ch in LINE_TERMINATORS:
            pos += 1
            continue
        if text.startswith("//", pos):
            pos += 2
            while pos < n and text[pos] not in LINE_TERMINATORS:
                pos += 1
            continue
        if text.startswith("/*", pos):
            end = text.find("*/", pos + 2)
            pos = n if end == -1 else end + 2
            continue
        if ch == '"' or ch == "'":
            quote = ch
            pos += 1
            while pos < n:
                c = text[pos]
                if c == "\\":
                    pos += 2
                    continue
                if c == quote:
                    pos += 1
                    break
                if c in LINE_TERMINATORS:
                    break
                pos += 1
            prev = "operand"
            continue
        if ch == "`":
            pos, inner = _scan_template(text, pos, identifiers, conservative)
            count += inner
            prev = "operand"
            continue
        if ch == "/":
            if not conservative and prev != "operand":
                regex_end = _try_parse_regex(text, pos)
                if regex_end is not None:
                    pos = regex_end
                    prev = "operand"
                    continue
            pos += 1
            prev = "operator"
            continue
        match = IDENT_RE.match(text, pos)
        if match:
            tok = match.group(0)
            pos = match.end()
            if tok[0].isdigit():
                prev = "operand"
            elif tok in VALUE_KEYWORDS:
                prev = "operand"
            elif tok in REGEX_ALLOWED_KEYWORDS:
                prev = "operator"
            else:
                if tok in identifiers:
                    count += 1
                prev = "operand"
            continue
        if ch == "{":
            if in_interpolation:
                depth += 1
            pos += 1
            prev = "operator"
            continue
        if ch == "}":
            if in_interpolation:
                depth -= 1
                pos += 1
                if depth == 0:
                    break
                prev = "operator"
                continue
            # Top level: ambiguous block-end vs object-end. Prefer regex-allowed
            # so a literal seam is skipped (fail-closed for the required file).
            pos += 1
            prev = "operator"
            continue
        if ch == ")" or ch == "]":
            pos += 1
            prev = "operand"
            continue
        # `(`, `[`, `,`, `;`, `:`, `?`, operators, `.`, `#`, `@`, and anything
        # else: expression-expected position, so a following `/` may be regex.
        pos += 1
        prev = "operator"
    return pos, count


def count_symbol(
    path: Path, symbol: str, *, for_forbidden_file: bool = False
) -> int | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    identifiers = SYMBOL_IDENTIFIERS.get(symbol, {symbol})
    _, count = _scan_js(
        text, 0, identifiers, conservative=for_forbidden_file,
        in_interpolation=False,
    )
    return count


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    parser.add_argument(
        "--root",
        default="",
        help="prefix for installed paths (test fixtures only; default is the real tree)",
    )
    args = parser.parse_args(argv)

    def resolve(p: str) -> Path:
        return Path(args.root + p) if args.root else Path(p)

    results: list[dict[str, Any]] = []
    failures = 0
    audit_errors = 0

    for key, raw_path, recorded, anchor, why in CITATIONS:
        path = resolve(raw_path)
        state, line, detail = find_anchor(path, anchor)

        if state == "error":
            verdict, audit_errors = "error", audit_errors + 1
        elif state == "absent":
            verdict, failures = "absent", failures + 1
        elif line != recorded:
            verdict, failures = "drifted", failures + 1
        else:
            verdict = "present"

        results.append(
            {
                "citation": key,
                "path": raw_path,
                "recorded_line": recorded,
                "actual_line": line,
                "verdict": verdict,
                "detail": detail,
                "why": why,
            }
        )

    present_results: list[dict[str, Any]] = []
    for symbol, why in PRESENT_EXPECTED:
        present_count = count_symbol(resolve(PRESENT_FILE), symbol)
        absent_count = count_symbol(
            resolve(ABSENT_FILE), symbol, for_forbidden_file=True
        )
        if present_count is None or absent_count is None:
            verdict, audit_errors = "error", audit_errors + 1
        elif present_count == 0:
            verdict, failures = "unexpectedly_absent", failures + 1
        elif absent_count != 0:
            verdict, failures = "unexpectedly_present_in_ssh", failures + 1
        else:
            verdict = "present_as_expected"
        present_results.append(
            {
                "symbol": symbol,
                "occurrences": present_count,
                "ssh_occurrences": absent_count,
                "verdict": verdict,
                "why": why,
            }
        )

    if args.json:
        json.dump(
            {
                "citations": results,
                "expected_present": present_results,
                "failures": failures,
                "audit_errors": audit_errors,
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    else:
        print("ADR citation gate — docs/native-ssh-os-authority-split.md")
        print()
        for r in results:
            mark = {"present": "ok  ", "drifted": "DRIFT", "absent": "ABSENT", "error": "ERROR"}[
                r["verdict"]
            ]
            actual = r["actual_line"] if r["actual_line"] is not None else "-"
            print(f"  [{mark}] {r['citation']}: recorded {r['recorded_line']}, actual {actual}")
            if r["verdict"] != "present":
                print(f"           {r['path']} — {r['detail']}")
        print()
        print("Symbols required PRESENT (case_e4054a66 seam deployed):")
        for r in present_results:
            mark = "ok  " if r["verdict"] == "present_as_expected" else "FAIL"
            print(f"  [{mark}] {r['symbol']}: {r['occurrences']} occurrence(s)")
            if r["verdict"] != "present_as_expected":
                print(f"           {r['verdict']}; ssh.ts: {r['ssh_occurrences']} occurrence(s)")
        print()
        print(f"failures={failures} audit_errors={audit_errors}")

    if audit_errors:
        return 2
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
