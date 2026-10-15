#!/usr/bin/env bash
# migration_squawk.sh — squawk + migration file-standard gate for CI
# (shared migration safety gates).
#
# WHAT IT PROVES. Every migration file under a migrations directory is
# checked against the org migration file standard (see
# docs/migration-standard.md) WITHOUT a database:
#   1. File name is NNNN_slug.sql (4-digit prefix, the same key the
#      migration ledger gate uses to match files to applied rows).
#   2. First comment block carries an expand/contract phase header:
#      `-- phase: expand` or `-- phase: contract` (other phases allowed,
#      but a phase must be declared).
#   3. CREATE INDEX uses CONCURRENTLY (a non-concurrent index build takes
#      a write lock for the whole build).
#   4. ADD CONSTRAINT uses NOT VALID; the matching VALIDATE CONSTRAINT is
#      expected in a later migration (expand/contract), never in the same
#      lock window — so VALIDATE in the same file is a warning, not a pass.
# Then, when a `squawk` binary is available (SQUAWK_BIN or PATH), each file
# is also linted by squawk itself and any squawk error fails the run. When
# squawk is absent the file-standard checks still run; the squawk step
# reports SKIP (exit 0) unless --strict, which refuses (exit 2) so a CI job
# that promised squawk cannot silently run without it.
#
# Usage:
#   migration_squawk.sh [--migrations-dir DIR] [--strict] [--squawk-bin PATH] [file.sql ...]
# With explicit files, only those are checked. Otherwise every *.sql under
# the migrations dir is checked. Default dir: ./migrations.
#
# Exit: 0 all pass (squawk SKIP allowed) | 1 a file-standard or squawk
# finding failed | 2 bad input / strict refusal (squawk missing).
# Offline: no database, no network. Never prints file contents beyond the
# offending line and its number.
set -euo pipefail
ME="$(basename "${BASH_SOURCE[0]}")"

DIR="migrations"
STRICT=0
SQUAWK_BIN="${SQUAWK_BIN:-}"

die() { echo "REFUSED: $ME: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --migrations-dir) DIR="${2:-}"; shift 2 ;;
    --strict) STRICT=1; shift ;;
    --squawk-bin) SQUAWK_BIN="${2:-}"; shift 2 ;;
    --) shift; break ;;
    -*) die "unknown flag: $1" ;;
    *) break ;;
  esac
done

FILES=()
if [[ $# -gt 0 ]]; then
  FILES=("$@")
else
  [[ -d "$DIR" ]] || die "no such migrations dir: $DIR (pass --migrations-dir or explicit files)"
  while IFS= read -r -d '' f; do FILES+=("$f"); done < <(find "$DIR" -maxdepth 1 -name '*.sql' -print0 | sort -z)
  [[ "${#FILES[@]}" -gt 0 ]] || die "no *.sql files in $DIR"
fi

FAIL=0
fail() { echo "FAIL: $ME: $1" >&2; FAIL=1; }
warn() { echo "WARN: $ME: $1" >&2; }

for f in "${FILES[@]}"; do
  [[ -r "$f" ]] || { echo "REFUSED: $ME: unreadable file: $f" >&2; exit 2; }
  base="$(basename "$f")"
  [[ "$base" =~ ^[0-9]{4}_[^/]+\.sql$ ]] \
    || { fail "$f: file name must be NNNN_slug.sql"; continue; }

  # Phase header: within the first 15 lines, a comment declaring the phase.
  if ! head -n 15 "$f" | grep -qiE '^[[:space:]]*--[[:space:]]*phase[[:space:]]*:'; then
    fail "$f: missing expand/contract phase header (e.g. '-- phase: expand')"
  fi

  # CREATE INDEX without CONCURRENTLY. Comment lines are excluded first so a
  # comment mentioning an index cannot fail or excuse a real statement.
  code="$(grep -viE '^[[:space:]]*--' "$f" || true)"
  if grep -qiE 'CREATE[[:space:]]+(UNIQUE[[:space:]]+)?INDEX' <<< "$code" \
    && ! grep -qiE 'CREATE[[:space:]]+(UNIQUE[[:space:]]+)?INDEX[[:space:]]+CONCURRENTLY' <<< "$code"; then
    bad_lines="$(grep -niE 'CREATE[[:space:]]+(UNIQUE[[:space:]]+)?INDEX' <<< "$code" | grep -vi 'concurrently' || true)"
    fail "$f: CREATE INDEX without CONCURRENTLY: ${bad_lines%%:*}"
  fi

  # ADD CONSTRAINT without NOT VALID (applies to FK / CHECK / UNIQUE /
  # EXCLUDE constraints added via ALTER TABLE ... ADD CONSTRAINT).
  if grep -qiE 'ADD[[:space:]]+CONSTRAINT' <<< "$code" \
    && ! grep -qiE 'ADD[[:space:]]+CONSTRAINT[^;]*NOT[[:space:]]+VALID' <<< "$code"; then
    fail "$f: ADD CONSTRAINT without NOT VALID (add NOT VALID, VALIDATE in a later migration)"
  fi
  if grep -qiE 'VALIDATE[[:space:]]+CONSTRAINT' <<< "$code" \
    && grep -qiE 'ADD[[:space:]]+CONSTRAINT' <<< "$code"; then
    warn "$f: same file both ADDs and VALIDATEs a constraint — split across expand/contract migrations"
  fi

  echo "ok: file standard: $f"
done

# Squawk itself, when available.
SQUAWK=""
if [[ -n "$SQUAWK_BIN" ]]; then
  [[ -x "$SQUAWK_BIN" ]] || die "--squawk-bin is not executable: $SQUAWK_BIN"
  SQUAWK="$SQUAWK_BIN"
elif command -v squawk >/dev/null 2>&1; then
  SQUAWK="squawk"
fi

if [[ -z "$SQUAWK" ]]; then
  if [[ "$STRICT" -eq 1 ]]; then
    echo "REFUSED: $ME: --strict with no squawk binary (SQUAWK_BIN or PATH)" >&2
    exit 2
  fi
  echo "SKIP: squawk binary absent — file-standard checks only"
else
  for f in "${FILES[@]}"; do
    if out="$("$SQUAWK" "$f" 2>&1)"; then
      echo "ok: squawk: $f"
    else
      fail "squawk reported on $f: $(head -n 3 <<< "$out" | tr '\n' ' ')"
    fi
  done
fi

exit "$FAIL"
