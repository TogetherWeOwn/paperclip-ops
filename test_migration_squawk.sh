#!/usr/bin/env bash
# ===========================================================================
# Offline suite for migration_squawk.sh (shared migration gates).
#
# Fully offline: fixture migrations in a throwaway temp dir, fake squawk
# binaries on PATH, no database, no network. Proves each file-standard rule
# fails by file name, the green path passes, and the squawk-missing policy
# (SKIP vs --strict REFUSED) holds. The fake squawk keeps the suite
# load-bearing when the real binary is absent: a suite that skips the
# squawk step whenever squawk is absent would pass while that step is
# broken, so both outcomes are driven with fakes that are always present.
# ===========================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$HERE/migration_squawk.sh"
PASS=0; FAIL=0
ok() { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/migs" "$WORK/bin"

cat > "$WORK/migs/0042_add_search_index.sql" <<'EOF'
-- phase: expand
CREATE INDEX CONCURRENTLY IF NOT EXISTS member_search_idx ON members (email);
ALTER TABLE members ADD CONSTRAINT members_email_format CHECK (email LIKE '%@%') NOT VALID;
EOF
cat > "$WORK/migs/0043_validate_email.sql" <<'EOF'
-- phase: contract
ALTER TABLE members VALIDATE CONSTRAINT members_email_format;
EOF
# Each rule-breaker isolates exactly one rule.
cat > "$WORK/bad_index.sql" <<'EOF'
-- phase: expand
CREATE INDEX member_lock_idx ON members (email);
EOF
cat > "$WORK/migs/0044_bad_constraint.sql" <<'EOF'
-- phase: expand
ALTER TABLE members ADD CONSTRAINT members_nick_unique UNIQUE (nickname);
EOF
cat > "$WORK/migs/0045_no_phase.sql" <<'EOF'
CREATE INDEX CONCURRENTLY member_other_idx ON members (id);
EOF

# Fake squawks: one green, one red naming the file.
cat > "$WORK/bin/squawk-ok" <<'FAKE'
#!/usr/bin/env bash
exit 0
FAKE
cat > "$WORK/bin/squawk-bad" <<'FAKE'
#!/usr/bin/env bash
echo "error: prefer-concurrent-index ($1)" >&2
exit 1
FAKE
chmod +x "$WORK/bin/squawk-ok" "$WORK/bin/squawk-bad"

# 1. Green pair passes, including the fake squawk.
"$GATE" --migrations-dir "$WORK/migs" --squawk-bin "$WORK/bin/squawk-ok" \
  "$WORK/migs/0042_add_search_index.sql" "$WORK/migs/0043_validate_email.sql" >/dev/null 2>&1 \
  && ok "green migrations pass file standard + squawk" \
  || bad "green migrations pass file standard + squawk"

# 2. Each rule fails naming its file.
"$GATE" "$WORK/bad_index.sql" >/dev/null 2>&1 \
  && bad "bad file name fails" || ok "bad file name fails"
out="$("$GATE" "$WORK/migs/0044_bad_constraint.sql" 2>&1 || true)"
grep -q "0044_bad_constraint.sql" <<< "$out" && grep -q "NOT VALID" <<< "$out" \
  && ok "ADD CONSTRAINT without NOT VALID fails naming the file" \
  || bad "ADD CONSTRAINT without NOT VALID fails naming the file"
out="$("$GATE" --squawk-bin "$WORK/bin/squawk-ok" "$WORK/migs/0045_no_phase.sql" 2>&1 || true)"
grep -q "0045_no_phase.sql" <<< "$out" && grep -qi "phase" <<< "$out" \
  && ok "missing phase header fails naming the file" \
  || bad "missing phase header fails naming the file"

# 3. Non-concurrent index fails even with a good name.
mkdir -p "$WORK/migs2"
cp "$WORK/bad_index.sql" "$WORK/migs2/0046_locking_index.sql"
out="$("$GATE" --squawk-bin "$WORK/bin/squawk-ok" "$WORK/migs2/0046_locking_index.sql" 2>&1 || true)"
grep -q "0046_locking_index.sql" <<< "$out" && grep -q "CONCURRENTLY" <<< "$out" \
  && ok "locking CREATE INDEX fails naming the file" \
  || bad "locking CREATE INDEX fails naming the file"

# A clean comment-only migration: mentions CREATE INDEX in a comment but
# has no real DDL, so it passes the file standard on its own.
mkdir -p "$WORK/migs3"
cat > "$WORK/migs3/0047_comment_only.sql" <<'EOF'
-- phase: expand
-- This replaces the old CREATE INDEX we dropped last release.
SELECT 1;
EOF

# 4. Squawk red fails the run; squawk absent SKIP vs --strict.
"$GATE" --squawk-bin "$WORK/bin/squawk-bad" "$WORK/migs/0042_add_search_index.sql" >/dev/null 2>&1 \
  && bad "red squawk fails the run" || ok "red squawk fails the run"
# Without squawk the clean dir must SKIP-pass (exit 0), while --strict
# still refuses the absent binary.
if PATH="$WORK/bin:/usr/bin:/bin" "$GATE" --migrations-dir "$WORK/migs3" >/dev/null 2>&1; then
  ok "absent squawk SKIP-passes without --strict"
else
  bad "absent squawk SKIP-passes without --strict"
fi
PATH="$WORK/bin:/usr/bin:/bin" "$GATE" --strict --migrations-dir "$WORK/migs3" >/dev/null 2>&1 \
  && bad "--strict refuses an absent squawk" || ok "--strict refuses an absent squawk"

# 5. A comment mentioning CREATE INDEX neither fails nor excuses.
"$GATE" --squawk-bin "$WORK/bin/squawk-ok" "$WORK/migs3/0047_comment_only.sql" >/dev/null 2>&1 \
  && ok "index mention in a comment is not a finding" \
  || bad "index mention in a comment is not a finding"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
