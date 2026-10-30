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
#      a write lock for the whole build). Checked PER STATEMENT: one
#      CONCURRENTLY build in a file never excuses a locking build in
#      another statement of the same file.
#   4. ADD CONSTRAINT uses NOT VALID; the matching VALIDATE CONSTRAINT is
#      expected in a later migration (expand/contract), never in the same
#      lock window — so VALIDATE in the same file is a warning, not a pass.
#      Also per statement, so multi-line constraint definitions are joined
#      before the check. `ADD ... USING INDEX` is exempt: a constraint built
#      from an existing index is valid by construction and takes no NOT
#      VALID clause.
# Then, when a `squawk` binary is available (SQUAWK_BIN or PATH), each file
# is also linted by squawk itself and any squawk error fails the run. Failure
# to run the shared SQL-check helper is itself a file-standard failure; the
# gate never treats a missing or crashed helper as a pass.
# Squawk runs with the canonical squawk-migration.toml next to this script
# (override with --squawk-config), which excludes ONLY require-lock-timeout
# and require-statement-timeout: timeout policy is enforced structurally by
# the preamble injector, not by pasted SET lines. When squawk is absent the
# file-standard checks still run; the squawk step reports SKIP (exit 0)
# unless --strict, which refuses (exit 2) so a CI job that promised squawk
# cannot silently run without it.
#
# Usage:
#   migration_squawk.sh [--migrations-dir DIR] [--strict] [--squawk-bin PATH]
#     [--squawk-config PATH] [--format text|json] [file.sql ...]
# --format json emits one schema_version=1 envelope (findings, squawk steps,
# summary counts). Text is the default; see docs/migration-standard.md.
# With explicit files, only those are checked. Otherwise every *.sql under
# the migrations dir is checked. Default dir: ./migrations.
# --squawk-config "" disables the canonical config (raw squawk defaults).
# SQUAWK_TIMEOUT_SECONDS overrides the 60s JSON-mode Squawk deadline.
#
# Exit: 0 all pass (squawk SKIP allowed) | 1 a file-standard or squawk
# finding failed | 2 bad input / strict refusal (squawk missing).
# Offline: no database, no network. Never prints file contents beyond the
# offending line and its number.
set -euo pipefail
ME="$(basename "${BASH_SOURCE[0]}")"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DIR="migrations"
STRICT=0
SQUAWK_BIN="${SQUAWK_BIN:-}"
SQUAWK_CONFIG="__default__"
FORMAT="text"

die() {
  if [[ "$FORMAT" == json ]]; then
    # Input values may contain credentials; JSON refusals use fixed messages.
    python3 "$HERE/migration_sql.py" refuse-json
  else
    echo "REFUSED: $ME: $*" >&2
  fi
  exit 2
}

while [[ $# -gt 0 ]]; do
  # Preserve legacy text parsing; JSON input refusals must still be envelopes.
  if [[ "$FORMAT" == json && $# -lt 2 ]]; then
    case "$1" in
      --migrations-dir|--squawk-bin|--squawk-config) die "missing option value" ;;
    esac
  fi
  case "$1" in
    --migrations-dir) DIR="${2:-}"; shift 2 ;;
    --strict) STRICT=1; shift ;;
    --squawk-bin) SQUAWK_BIN="${2:-}"; shift 2 ;;
    --squawk-config) SQUAWK_CONFIG="${2:-}"; shift 2 ;;
    --format)
      [[ $# -ge 2 ]] || die "--format requires text or json"
      case "$2" in
        text|json) FORMAT="$2" ;;
        *) die "--format requires text or json" ;;
      esac
      shift 2 ;;
    --) shift; break ;;
    -*) die "unknown flag: $1" ;;
    *) break ;;
  esac
done

if [[ "$FORMAT" == json ]]; then
  exec python3 "$HERE/migration_sql.py" gate-json "$DIR" "$STRICT" \
    "$SQUAWK_BIN" "$SQUAWK_CONFIG" "$@"
fi

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
  # Capture-first (no head | grep -q pipeline): piping into an early-exiting
  # grep under pipefail can report a match as a miss (see
  # verification/case_c25c4d3f-sigpipe-verdict-gate.sh).
  header="$(head -n 15 "$f")"
  if ! grep -qiE '^[[:space:]]*--[[:space:]]*phase[[:space:]]*:' <<<"$header"; then
    fail "$f: missing expand/contract phase header (e.g. '-- phase: expand')"
  fi

  # Per-statement checks. Statements are split on semicolons outside
  # single-quoted strings with -- comments stripped first, so a comment
  # mentioning an index can neither fail nor excuse a real statement, and a
  # multi-line constraint definition is judged whole. Each CREATE INDEX
  # statement needs its own CONCURRENTLY; each ADD CONSTRAINT statement
  # needs its own NOT VALID (USING INDEX constraints are exempt: valid by
  # construction, NOT VALID is not accepted there).
  FILE_FAIL="$FAIL"
  HAS_ADD=0; HAS_VALIDATE=0
  if ! CHECK_OUTPUT="$(python3 "$HERE/migration_sql.py" check "$f")"; then
    fail "$f: migration_sql.py check failed"
    CHECK_OUTPUT=""
  fi
  while IFS='|' read -r level line text; do
    case "$level" in
      FAIL) fail "$f:$line: $text" ;;
      META) [[ "$text" == ADD_CONSTRAINT ]] && HAS_ADD=1
            [[ "$text" == VALIDATE_CONSTRAINT ]] && HAS_VALIDATE=1 ;;
    esac
  done <<<"$CHECK_OUTPUT"
  if [[ "$HAS_ADD" -eq 1 && "$HAS_VALIDATE" -eq 1 ]]; then
    warn "$f: same file both ADDs and VALIDATEs a constraint — split across expand/contract migrations"
  fi
  # A file whose statements all pass keeps its green line for the log;
  # a file with findings already named them above.
  if [[ "$FAIL" -eq "$FILE_FAIL" ]]; then
    echo "ok: file standard: $f"
  fi
done

# Squawk itself, when available.
SQUAWK=""
if [[ -n "$SQUAWK_BIN" ]]; then
  [[ -x "$SQUAWK_BIN" ]] || die "--squawk-bin is not executable: $SQUAWK_BIN"
  SQUAWK="$SQUAWK_BIN"
elif command -v squawk >/dev/null 2>&1; then
  SQUAWK="squawk"
fi

if [[ "$SQUAWK_CONFIG" == "__default__" ]]; then
  if [[ -r "$HERE/squawk-migration.toml" ]]; then
    SQUAWK_CONFIG="$HERE/squawk-migration.toml"
  else
    SQUAWK_CONFIG=""
  fi
fi
if [[ -n "$SQUAWK_CONFIG" && ! -r "$SQUAWK_CONFIG" ]]; then
  die "--squawk-config is not readable: $SQUAWK_CONFIG"
fi

if [[ -z "$SQUAWK" ]]; then
  if [[ "$STRICT" -eq 1 ]]; then
    echo "REFUSED: $ME: --strict with no squawk binary (SQUAWK_BIN or PATH)" >&2
    exit 2
  fi
  echo "SKIP: squawk binary absent — file-standard checks only"
else
  SQUAWK_ARGS=()
  if [[ -n "$SQUAWK_CONFIG" ]]; then
    SQUAWK_ARGS=(--config "$SQUAWK_CONFIG")
  fi
  if [[ -n "$SQUAWK_CONFIG" ]]; then
    echo "squawk config: $SQUAWK_CONFIG"
  else
    echo "squawk config: none (raw squawk defaults)"
  fi
  for f in "${FILES[@]}"; do
    if out="$("$SQUAWK" "${SQUAWK_ARGS[@]}" "$f" 2>&1)"; then
      echo "ok: squawk: $f"
    else
      fail "squawk reported on $f: $(head -n 3 <<< "$out" | tr '\n' ' ')"
    fi
  done
fi

exit "$FAIL"
