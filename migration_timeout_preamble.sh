#!/usr/bin/env bash
# migration_timeout_preamble.sh — canonical lock/statement timeout preamble
# for database migrations (shared migration safety gates).
#
# WHY THIS EXISTS. A migration that waits forever on a lock blocks deploys
# and can stall production traffic behind it. Every migration in this org
# runs with a short lock_timeout (fail fast on lock contention) and a bounded
# statement_timeout (no runaway DDL). The deploy runner injects this preamble
# at apply time, so migration files stay clean; CI verifies the injection
# rather than requiring authors to paste it.
#
# Usage:
#   migration_timeout_preamble.sh print [--lock-timeout 10s] [--statement-timeout 120s]
#       Emit the preamble SQL to stdout.
#   migration_timeout_preamble.sh check <file.sql> [...]
#       Exit 0 when every file starts with (or contains) the preamble SET
#       statements; exit 1 naming the files that do not.
#   migration_timeout_preamble.sh inject <file.sql> > out.sql
#       Emit <file.sql> with the preamble prepended. Idempotent: a file that
#       already carries the preamble is passed through unchanged.
#
# CONCURRENTLY EXEMPTION. A bounded statement_timeout cancels CREATE INDEX
# CONCURRENTLY on large tables mid-build and leaves an INVALID index behind;
# worse, an `IF NOT EXISTS` on the retry then reports success while
# indisvalid stays false (reproduced on Postgres 18). So inject wraps every
# concurrent index build: statement_timeout is lifted to '0' (disabled) for
# that statement only and restored to the configured value right after.
# lock_timeout stays in force throughout — failing fast on lock contention
# is exactly what a concurrent build should do. Authors must NOT write
# `IF NOT EXISTS` on concurrent indexes (the file standard rejects nothing
# here, but docs/migration-standard.md bans it) precisely so a retry can
# never mask a failed build.
#
# Exit: 0 ok | 1 check failed | 2 bad input / unreadable file (REFUSED).
# Offline: reads files only, never touches a database, prints no secrets.
set -euo pipefail
ME="$(basename "${BASH_SOURCE[0]}")"

LOCK_TIMEOUT="10s"
STATEMENT_TIMEOUT="120s"

die() { echo "REFUSED: $ME: $*" >&2; exit 2; }

# parse_timeouts consumes leading flags from "$@" and leaves the rest in
# the REMAIN array (a function cannot shift its caller's "$@" for it).
parse_timeouts() {
  REMAIN=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --lock-timeout) LOCK_TIMEOUT="${2:-}"; shift 2 ;;
      --statement-timeout) STATEMENT_TIMEOUT="${2:-}"; shift 2 ;;
      --) shift; while [[ $# -gt 0 ]]; do REMAIN+=("$1"); shift; done; break ;;
      -*) die "unknown flag: $1" ;;
      *) REMAIN+=("$1"); shift ;;
    esac
  done
  [[ "$LOCK_TIMEOUT" =~ ^[0-9]+(ms|s|min)$ ]] || die "bad --lock-timeout: $LOCK_TIMEOUT"
  [[ "$STATEMENT_TIMEOUT" =~ ^[0-9]+(ms|s|min)$ ]] || die "bad --statement-timeout: $STATEMENT_TIMEOUT"
}

print_preamble() {
  printf "SET lock_timeout = '%s';\nSET statement_timeout = '%s';\n" \
    "$LOCK_TIMEOUT" "$STATEMENT_TIMEOUT"
}

# has_preamble <file>: 0 when both SET statements are present (any order,
# single or double quotes, any value — the runner pins values, this proves
# presence, not equality).
has_preamble() {
  grep -qiE "^[[:space:]]*SET[[:space:]]+lock_timeout[[:space:]]*=" "$1" \
    && grep -qiE "^[[:space:]]*SET[[:space:]]+statement_timeout[[:space:]]*=" "$1"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  print)
    parse_timeouts "$@"; set -- "${REMAIN[@]}"
    [[ $# -eq 0 ]] || die "print takes no file arguments"
    print_preamble
    ;;
  check)
    parse_timeouts "$@"; set -- "${REMAIN[@]}"
    [[ $# -ge 1 ]] || die "check needs at least one file"
    rc=0
    for f in "$@"; do
      [[ -r "$f" ]] || { echo "REFUSED: $ME: unreadable file: $f" >&2; exit 2; }
      if has_preamble "$f"; then
        echo "ok: preamble present: $f"
      else
        echo "FAIL: missing timeout preamble: $f"
        rc=1
      fi
    done
    exit "$rc"
    ;;
  inject)
    parse_timeouts "$@"; set -- "${REMAIN[@]}"
    [[ $# -eq 1 ]] || die "inject takes exactly one file"
    [[ -r "$1" ]] || die "unreadable file: $1"
    if has_preamble "$1"; then
      cat "$1"
    else
      print_preamble
      python3 - "$1" <<EOF
import re
import sys
stmt = "$STATEMENT_TIMEOUT"
src = open(sys.argv[1], encoding="utf-8", errors="replace").read()
# Split on ';' outside single-quoted strings (same statement rule as
# migration_squawk.sh); comments ride along inside their statement.
parts, buf = [], []
i, n, in_str = 0, len(src), False
while i < n:
    ch = src[i]
    if in_str:
        buf.append(ch)
        if ch == "'":
            if i + 1 < n and src[i + 1] == "'":
                buf.append("'")
                i += 2
                continue
            in_str = False
        i += 1
        continue
    if ch == "'":
        in_str = True
        buf.append(ch)
        i += 1
        continue
    if ch == ";":
        buf.append(ch)
        parts.append("".join(buf))
        buf = []
        i += 1
        continue
    buf.append(ch)
    i += 1
tail = "".join(buf)
if tail:
    parts.append(tail)
out = []
for part in parts:
    if re.search(r"\bCREATE\s+(UNIQUE\s+)?INDEX\s+CONCURRENTLY\b", part, re.I):
        out.append("SET statement_timeout = '0';\n")
        out.append(part)
        if not part.rstrip().endswith(";"):
            out.append(";")
        out.append("\nSET statement_timeout = '%s';\n" % stmt)
    else:
        out.append(part)
sys.stdout.write("".join(out))
EOF
    fi
    ;;
  *) cat >&2 <<EOF
Usage: $ME {print|check|inject} [options] [files]
  print [--lock-timeout 10s] [--statement-timeout 120s]
  check <file.sql> [...]
  inject <file.sql> > out.sql
EOF
    exit 2
    ;;
esac
