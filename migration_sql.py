#!/usr/bin/env python3
"""Small PostgreSQL-aware statement lexer shared by migration gates."""
import re
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


def check(path):
    source = Path(path).read_text(encoding="utf-8", errors="replace")
    for line, _raw, code in split_statements(source):
        if not code.strip():
            continue
        if re.search(r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b", code, re.I) and not re.search(r"\bCONCURRENTLY\b", code, re.I):
            print(f"FAIL|{line}|CREATE INDEX without CONCURRENTLY")
        if re.search(r"\bADD\s+CONSTRAINT\b", code, re.I):
            if not re.search(r"\bUSING\s+INDEX\b", code, re.I) and not re.search(r"\bNOT\s+VALID\b", code, re.I):
                print(f"FAIL|{line}|ADD CONSTRAINT without NOT VALID (add NOT VALID, VALIDATE in a later migration)")
        # Expose per-file warning via a sentinel; caller emits it once.
        if re.search(r"\bADD\s+CONSTRAINT\b", code, re.I):
            print(f"META|{line}|ADD_CONSTRAINT")
        if re.search(r"\bVALIDATE\s+CONSTRAINT\b", code, re.I):
            print(f"META|{line}|VALIDATE_CONSTRAINT")


if __name__ == "__main__":
    action, *args = sys.argv[1:]
    if action == "inject" and len(args) == 2:
        inject(args[0], args[1])
    elif action == "check" and len(args) == 1:
        check(args[0])
    else:
        sys.exit("usage: migration_sql.py {inject FILE TIMEOUT|check FILE}")
