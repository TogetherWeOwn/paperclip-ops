#!/usr/bin/env bash
# ===========================================================================
# Offline suite for migration_timeout_preamble.sh (shared migration gates).
#
# Fully offline: fixture files in a throwaway temp dir, no database, no
# network, no credentials. Proves print/check/inject plus the refusal paths.
# ===========================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$HERE/migration_timeout_preamble.sh"
PASS=0; FAIL=0
ok() { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

# 1. print emits both SET statements with defaults. A failed print leaves a
# sentinel so the content assertions below fail as findings, not as an abort.
out="$("$GATE" print)" || out="PRINT-FAILED"
grep -q "SET lock_timeout = '10s';" <<< "$out" && grep -q "SET statement_timeout = '120s';" <<< "$out" \
  && ok "print emits default lock/statement timeouts" \
  || bad "print emits default lock/statement timeouts"

# 2. print honors overrides and refuses garbage.
out="$("$GATE" print --lock-timeout 5s --statement-timeout 30s)" || out="PRINT-FAILED"
grep -q "SET lock_timeout = '5s';" <<< "$out" && grep -q "SET statement_timeout = '30s';" <<< "$out" \
  && ok "print honors timeout overrides" \
  || bad "print honors timeout overrides"
"$GATE" print --lock-timeout forever >/dev/null 2>&1 \
  && bad "print refuses a bad timeout" || ok "print refuses a bad timeout"

# 3. check passes a file with the preamble, fails one without — naming it.
printf "SET lock_timeout = '10s';\nSET statement_timeout = '120s';\nSELECT 1;\n" > "$WORK/good.sql"
printf "SELECT 1;\n" > "$WORK/bare.sql"
"$GATE" check "$WORK/good.sql" >/dev/null 2>&1 \
  && ok "check passes a file carrying the preamble" \
  || bad "check passes a file carrying the preamble"
bare_out="$("$GATE" check "$WORK/bare.sql" 2>&1 || true)"
if "$GATE" check "$WORK/bare.sql" >/dev/null 2>&1; then
  bad "check fails a file without the preamble"
elif grep -q "bare.sql" <<< "$bare_out"; then
  ok "check fails a file without the preamble, naming it"
else
  bad "check fails a file without the preamble, naming it"
fi
"$GATE" check "$WORK/missing.sql" >/dev/null 2>&1 \
  && bad "check refuses an unreadable file" || ok "check refuses an unreadable file"

# 4. inject prepends once and is idempotent.
"$GATE" inject "$WORK/bare.sql" > "$WORK/wrapped.sql"
# Capture-first (no head | grep -q pipeline): piping into an early-exiting
# grep under pipefail can report a match as a miss (see
# verification/case_c25c4d3f-sigpipe-verdict-gate.sh).
wrapped_first_line="$(head -n 1 "$WORK/wrapped.sql")"
grep -q "SET lock_timeout" <<<"$wrapped_first_line" \
  && grep -q "SELECT 1;" "$WORK/wrapped.sql" \
  && ok "inject prepends the preamble ahead of the body" \
  || bad "inject prepends the preamble ahead of the body"
"$GATE" inject "$WORK/wrapped.sql" > "$WORK/twice.sql"
[[ "$(grep -c "SET lock_timeout" "$WORK/twice.sql")" -eq 1 ]] \
  && ok "inject is idempotent (no doubled preamble)" \
  || bad "inject is idempotent (no doubled preamble)"

# 5. CREATE INDEX CONCURRENTLY is wrapped: statement_timeout is lifted for
# that statement only (a 120s bound would cancel the build on large tables
# and leave an INVALID index), then restored; lock_timeout stays on.
cat > "$WORK/cic.sql" <<'EOF'
-- phase: expand
CREATE INDEX CONCURRENTLY member_search_idx ON members (email);
ALTER TABLE members ADD CONSTRAINT c CHECK (email LIKE '%@%') NOT VALID;
EOF
"$GATE" inject "$WORK/cic.sql" > "$WORK/cic.wrapped.sql"
if grep -q "SET statement_timeout = '0';" "$WORK/cic.wrapped.sql" \
  && grep -q "SET statement_timeout = '120s';" "$WORK/cic.wrapped.sql" \
  && grep -q "SET lock_timeout = '10s';" "$WORK/cic.wrapped.sql"; then
  # The lift must come before the CIC statement and a restore after it
  # (the first 120s line is the preamble itself, so search past the CIC).
  lift_line="$(grep -n "SET statement_timeout = '0';" "$WORK/cic.wrapped.sql" | head -n 1 | cut -d: -f1)"
  cic_line="$(grep -n "CREATE INDEX CONCURRENTLY" "$WORK/cic.wrapped.sql" | head -n 1 | cut -d: -f1)"
  # Capture-first (no tail | grep -q pipeline): piping into an early-exiting
  # grep under pipefail can report a match as a miss (see
  # verification/case_c25c4d3f-sigpipe-verdict-gate.sh).
  cic_tail="$(tail -n +"$cic_line" "$WORK/cic.wrapped.sql")"
  if [[ "$lift_line" -lt "$cic_line" ]] \
    && grep -q "SET statement_timeout = '120s';" <<<"$cic_tail"; then
    ok "inject lifts statement_timeout around CONCURRENTLY builds only"
  else
    bad "inject lifts statement_timeout around CONCURRENTLY builds only"
  fi
else
  bad "inject lifts statement_timeout around CONCURRENTLY builds only"
fi
# A plain statement keeps the bounded timeout (no lift, no restore noise).
"$GATE" inject "$WORK/bare.sql" > "$WORK/bare.wrapped.sql"
if ! grep -q "statement_timeout = '0'" "$WORK/bare.wrapped.sql" \
  && [[ "$(grep -c "SET statement_timeout" "$WORK/bare.wrapped.sql")" -eq 1 ]]; then
  ok "inject leaves non-concurrent statements under the bound"
else
  bad "inject leaves non-concurrent statements under the bound"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
