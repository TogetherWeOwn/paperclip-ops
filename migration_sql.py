#!/usr/bin/env python3
"""Small PostgreSQL-aware statement lexer shared by migration gates."""
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def split_statements(source):
    """Return raw statement chunks and comment-masked SQL with line numbers."""
    chunks = []
    raw, code = [], []
    line = 1
    start = None
    i, n = 0, len(source)
    state = "normal"
    dollar_end = ""
    block_depth = 0
    escape_string = False

    def append(ch, visible=True):
        nonlocal start
        raw.append(ch)
        code.append(ch if visible else ("\n" if ch == "\n" else " "))
        if visible and start is None and not ch.isspace():
            start = line

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if state == "line_comment":
            append(ch, False)
            i += 1
            if ch == "\n":
                line += 1
                state = "normal"
            continue
        if state == "block_comment":
            append(ch, False)
            if ch == "/" and nxt == "*":
                append(nxt, False); block_depth += 1; i += 2; continue
            if ch == "*" and nxt == "/":
                append(nxt, False); block_depth -= 1; i += 2
                if block_depth == 0:
                    state = "normal"
                continue
            if ch == "\n": line += 1
            i += 1
            continue
        if state in ("single", "escape_single"):
            append(ch)
            if state == "escape_single" and ch == "\\" and i + 1 < n:
                append(nxt); i += 2
                if nxt == "\n": line += 1
                continue
            if ch == "'":
                if nxt == "'":
                    append(nxt); i += 2; continue
                state = "normal"
            if ch == "\n": line += 1
            i += 1
            continue
        if state == "double":
            append(ch)
            if ch == '"':
                if nxt == '"': append(nxt); i += 2; continue
                state = "normal"
            if ch == "\n": line += 1
            i += 1
            continue
        if state == "dollar":
            if source.startswith(dollar_end, i):
                for c in dollar_end: append(c)
                i += len(dollar_end); state = "normal"; continue
            append(ch)
            if ch == "\n": line += 1
            i += 1
            continue

        if ch == "-" and nxt == "-":
            append(ch, False); append(nxt, False); i += 2; state = "line_comment"; continue
        if ch == "/" and nxt == "*":
            append(ch, False); append(nxt, False); i += 2; state = "block_comment"; block_depth = 1; continue
        if ch == "'":
            # PostgreSQL E'...' strings escape backslashes; ordinary strings
            # still honor doubled quotes but do not treat backslash as escape.
            prev = source[i - 1] if i else ""
            before = source[i - 2] if i > 1 else ""
            escape = prev in "eE" and (not before or not (before.isalnum() or before in "_$"))
            append(ch); state = "escape_single" if escape else "single"; i += 1; continue
        if ch == '"': append(ch); state = "double"; i += 1; continue
        if ch == "$":
            m = re.match(r"\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$", source[i:])
            if m:
                dollar_end = m.group(0)
                for c in dollar_end: append(c)
                i += len(dollar_end); state = "dollar"; continue
        if ch == ";":
            statement_start = start
            append(ch)
            chunks.append((statement_start or line, "".join(raw), "".join(code)))
            raw, code = [], []
            start = None
            i += 1
            continue
        append(ch)
        if ch == "\n": line += 1
        i += 1

    if raw and "".join(raw).strip():
        chunks.append((start, "".join(raw), "".join(code)))
    return chunks


def inject(path, timeout):
    source = Path(path).read_text(encoding="utf-8", errors="replace")
    output = []
    for _line, raw, code in split_statements(source):
        if re.search(r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY\b", code, re.I):
            output.append("SET statement_timeout = '0';\n")
            output.append(raw)
            if not raw.rstrip().endswith(";"):
                output.append(";\n")
            output.append(f"\nSET statement_timeout = '{timeout}';\n")
        else:
            output.append(raw)
    sys.stdout.write("".join(output))


def statement_checks(source):
    """Share the exact rule evaluation between prose and JSON consumers."""
    for line, _raw, code in split_statements(source):
        if not code.strip():
            continue
        if re.search(r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b", code, re.I) and not re.search(r"\bCONCURRENTLY\b", code, re.I):
            yield "FAIL", "concurrent-index", line, "CREATE INDEX without CONCURRENTLY"
        if re.search(r"\bADD\s+CONSTRAINT\b", code, re.I):
            if not re.search(r"\bUSING\s+INDEX\b", code, re.I) and not re.search(r"\bNOT\s+VALID\b", code, re.I):
                yield "FAIL", "not-valid-constraint", line, "ADD CONSTRAINT without NOT VALID (add NOT VALID, VALIDATE in a later migration)"
        # Expose per-file warning via a sentinel; caller emits it once.
        if re.search(r"\bADD\s+CONSTRAINT\b", code, re.I):
            yield "META", "", line, "ADD_CONSTRAINT"
        if re.search(r"\bVALIDATE\s+CONSTRAINT\b", code, re.I):
            yield "META", "", line, "VALIDATE_CONSTRAINT"


def check(path):
    source = Path(path).read_text(encoding="utf-8", errors="replace")
    for level, _rule, line, message in statement_checks(source):
        print(f"{level}|{line}|{message}")


SQUAWK_SCHEMA_VERSION = 1
DIAGNOSTIC_LIMIT = 2000


def emit_gate_json(files, error=None):
    """Derive every count and verdict from the emitted per-file results."""
    for result in files:
        result["verdict"] = "fail" if (
            any(f["severity"] == "error" for f in result["findings"])
            or result["squawk"]["status"] == "fail"
        ) else "pass"
    failed = sum(f["verdict"] == "fail" for f in files)
    verdict = "refused" if error else "fail" if failed else "pass"
    envelope = {
        "schema_version": SQUAWK_SCHEMA_VERSION,
        "verdict": verdict,
        "files": files,
        "summary": {
            "total_files": len(files),
            "failed_files": failed,
            "error_findings": sum(f["severity"] == "error" for r in files for f in r["findings"]),
            "warning_findings": sum(f["severity"] == "warning" for r in files for f in r["findings"]),
            "squawk": {key: sum(r["squawk"]["status"] == value for r in files)
                       for key, value in (("passed", "pass"), ("failed", "fail"), ("skipped", "skip"))},
        },
    }
    if error:
        envelope["error"] = {"code": error[0], "message": error[1]}
    print(json.dumps(envelope))
    return {"pass": 0, "fail": 1, "refused": 2}[verdict]


def squawk_step(argv):
    """Drain output without buffering it all; publish tags, never SQL snippets.

    Squawk's pretty diagnostics include source lines, identifiers and literals.
    Only severity/rule tags from diagnostic headers are safe passthrough. An
    opaque diagnostic is represented by a fixed marker, not echoed as prose.
    """
    prefix = ""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
        while chunk := proc.stdout.read(8192):
            text = decoder.decode(chunk)
            if len(prefix) <= DIAGNOSTIC_LIMIT:
                prefix += text[:DIAGNOSTIC_LIMIT + 1 - len(prefix)]
        prefix += decoder.decode(b"", final=True)[:max(0, DIAGNOSTIC_LIMIT + 1 - len(prefix))]
        rc = proc.wait()
    truncated = len(prefix) > DIAGNOSTIC_LIMIT
    bounded = prefix[:DIAGNOSTIC_LIMIT]
    bounded = re.sub(r"\x1b\[[0-9;]*m", "", bounded)
    tags = []
    for line in bounded.splitlines():
        match = re.match(r"\s*(error|warning)(?:\[([a-z]+(?:-[a-z]+)+)\]|:\s*([a-z]+(?:-[a-z]+)+)(?=[\s:(]|$))", line)
        if match:
            tags.append(f"{match[1]}: {match[2] or match[3]}")
    diagnostics = "\n".join(tags) if tags else "[unstructured diagnostics omitted]" if prefix else ""
    return {"status": "pass" if rc == 0 else "fail",
            "diagnostics": diagnostics[:DIAGNOSTIC_LIMIT], "truncated": truncated or len(diagnostics) > DIAGNOSTIC_LIMIT}


def gate_json(directory, strict, squawk_bin, squawk_config, paths):
    """JSON orchestration for migration_squawk.sh, with unchanged gate policy."""
    files = []

    def refuse(code, message):
        return emit_gate_json(files, (code, message))

    if not paths:
        if not Path(directory).is_dir():
            return refuse("migrations-dir-missing", "Migrations directory does not exist.")
        try:
            paths = [os.path.join(directory, name) for name in sorted(os.listdir(directory)) if name.endswith(".sql")]
        except OSError:
            return refuse("migrations-dir-unreadable", "Cannot list the migrations directory.")
        if not paths:
            return refuse("migrations-empty", "No SQL migration files found.")
    for path in paths:
        try:
            source = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return refuse("file-unreadable", "Cannot read a migration file.")
        findings = []

        def finding(rule, message, line=None, severity="error"):
            findings.append({"rule": rule, "line": line, "severity": severity, "message": message})

        # Like text mode, a bad name short-circuits the other file checks,
        # but not the later squawk step.
        if not re.fullmatch(r"[0-9]{4}_[^/]+\.sql", Path(path).name):
            finding("file-name", "File name must be NNNN_slug.sql")
        else:
            if not re.search(r"^[^\S\n]*--[^\S\n]*phase[^\S\n]*:", "\n".join(source.split("\n")[:15]), re.I | re.M):
                finding("phase-header", "Missing expand/contract phase header")
            meta = set()
            for level, rule, line, message in statement_checks(source):
                if level == "FAIL":
                    finding(rule, message, line)
                else:
                    meta.add(message)
            if {"ADD_CONSTRAINT", "VALIDATE_CONSTRAINT"} <= meta:
                finding("split-add-validate", "Same file both ADDs and VALIDATEs a constraint; split across expand/contract migrations", severity="warning")
        files.append({"file": path, "findings": findings,
                      "squawk": {"status": "skip", "diagnostics": "", "truncated": False}})

    squawk = squawk_bin or shutil.which("squawk")
    if squawk_bin and not os.access(squawk_bin, os.X_OK):
        return refuse("squawk-not-executable", "Configured squawk binary is not executable.")
    if squawk_config == "__default__":
        default = Path(__file__).with_name("squawk-migration.toml")
        squawk_config = str(default) if os.access(default, os.R_OK) else ""
    if squawk_config and not os.access(squawk_config, os.R_OK):
        return refuse("squawk-config-unreadable", "Squawk config is not readable.")
    if not squawk:
        if strict == "1":
            return refuse("squawk-missing", "Strict mode requires a squawk binary.")
    else:
        args = [squawk] + (["--config", squawk_config] if squawk_config else [])
        for result in files:
            try:
                result["squawk"] = squawk_step([*args, result["file"]])
            except OSError:
                # An executable that cannot launch is still a failed squawk
                # step (same exit 1 as the shell's conditional invocation).
                result["squawk"] = {"status": "fail", "diagnostics": "Squawk could not execute.", "truncated": False}
    return emit_gate_json(files)


if __name__ == "__main__":
    action, *args = sys.argv[1:]
    if action == "inject" and len(args) == 2:
        inject(args[0], args[1])
    elif action == "check" and len(args) == 1:
        check(args[0])
    elif action == "refuse-json" and not args:
        sys.exit(emit_gate_json([], ("invalid-input", "Invalid gate arguments.")))
    elif action == "gate-json" and len(args) >= 4:
        sys.exit(gate_json(*args[:4], args[4:]))
    else:
        sys.exit("usage: migration_sql.py {inject FILE TIMEOUT|check FILE|gate-json DIR STRICT BIN CONFIG [FILE ...]}")
